# ActivitySim
# See full license in LICENSE.txt.

import logging

import numpy as np
import pandas as pd

from activitysim.core.interaction_sample_simulate import interaction_sample_simulate
from activitysim.core import tracing
from activitysim.core import inject
from activitysim.core import logit

logger = logging.getLogger(__name__)


def get_previous_tour_by_tourid(current_tour_person_ids,
                                previous_tour_by_personid,
                                alts):
    """
    Matches current tours with attributes of previous tours for the same
    person.  See the return value below for more information.

    Parameters
    ----------
    current_tour_person_ids : Series
        A Series of person ids for the tours we're about make the choice for
        - index should match the tours DataFrame.
    previous_tour_by_personid : Series
        A Series where the index is the person id and the value is the index
        of the alternatives of the scheduling.
    alts : DataFrame
        The alternatives of the scheduling.

    Returns
    -------
    prev_alts : DataFrame
        A DataFrame with an index matching the CURRENT tours we're making a
        decision for, but with columns from the PREVIOUS tour of the person
        associated with each of the CURRENT tours.  Columns listed in PREV_TOUR_COLUMNS
        from the alternatives will have "_previous" added as a suffix to keep
        differentiated from the current alternatives that will be part of the
        interaction.
    """

    PREV_TOUR_COLUMNS = ['start', 'end']

    previous_tour_by_tourid = \
        previous_tour_by_personid.loc[current_tour_person_ids]

    previous_tour_by_tourid = alts.loc[previous_tour_by_tourid, PREV_TOUR_COLUMNS]

    previous_tour_by_tourid.index = current_tour_person_ids.index
    previous_tour_by_tourid.columns = [x+'_previous' for x in PREV_TOUR_COLUMNS]

    return previous_tour_by_tourid


def tdd_interaction_dataset(tours, alts, timetable, choice_column):
    """
    interaction_sample_simulate expects
    alts index same as choosers (e.g. tour_id)
    name of choice column in alts

    Parameters
    ----------
    tours : pandas DataFrame
        must have person_id column and index on tour_id
    alts : pandas DataFrame
        alts index must be timetable tdd id
    timetable : TimeTable object
    choice_column : str
        name of column to store alt index in alt_tdd DataFrame
        (since alt_tdd is duplicate index on person_id but unique on person_id,alt_id)

    Returns
    -------
    alt_tdd : pandas DataFrame
        columns: start, end , duration, <choice_column>
        index: tour_id


    """

    alts_ids = np.tile(alts.index, len(tours.index))
    tour_ids = np.repeat(tours.index, len(alts.index))
    person_ids = np.repeat(tours['person_id'], len(alts.index))

    alt_tdd = alts.take(alts_ids).copy()
    alt_tdd.index = tour_ids
    alt_tdd['person_id'] = person_ids
    alt_tdd[choice_column] = alts_ids

    # slice out all non-available tours
    available = timetable.tour_available(alt_tdd.person_id, alt_tdd[choice_column])

    assert available.any()

    alt_tdd = alt_tdd[available]

    # FIXME - don't need this any more after slicing
    del alt_tdd['person_id']

    return alt_tdd


def schedule_tours(tours, persons_merged,
                   alts, spec, constants,
                   timetable, previous_tour_by_personid,
                   chunk_size, tour_trace_label):

    logger.info("%s schedule_tours running %d tour choices" % (tour_trace_label, len(tours)))

    # timetable can't handle multiple tours per person
    assert len(tours.index) == len(np.unique(tours.person_id.values))

    # merge persons into tours
    tours = pd.merge(tours, persons_merged, left_on='person_id', right_index=True)

    # merge previous tour columns
    tours = tours.join(
        get_previous_tour_by_tourid(tours.person_id, previous_tour_by_personid, alts)
    )

    # build interaction dataset filtered to include only available tdd alts
    # dataframe columns start, end , duration, person_id, tdd
    # indexed (not unique) on tour_id
    choice_column = 'tdd'
    alt_tdd = tdd_interaction_dataset(tours, alts, timetable, choice_column=choice_column)

    locals_d = {
        'tt': timetable
    }
    if constants is not None:
        locals_d.update(constants)

    choices = interaction_sample_simulate(
        tours,
        alt_tdd,
        spec,
        choice_column=choice_column,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=tour_trace_label
    )

    previous_tour_by_personid.loc[tours.person_id] = choices.values

    timetable.assign(tours.person_id, choices)

    return choices


def vectorize_tour_scheduling(tours, persons_merged, alts, spec,
                              constants={},
                              chunk_size=0, trace_label=None):
    """
    The purpose of this method is fairly straightforward - it takes tours
    and schedules them into time slots.  Alternatives should be specified so
    as to define those time slots (usually with start and end times).

    The difficulty of doing this in Python is that subsequent tours are
    dependent on certain characteristics of previous tours for the same
    person.  This is a problem with Python's vectorization requirement,
    so this method does all the 1st tours, then all the 2nd tours, and so forth.

    This method also adds variables that can be used in the spec which have
    to do with the previous tours per person.  Every column in the
    alternatives table is appended with the suffix "_previous" and made
    available.  So if your alternatives table has columns for start and end,
    then start_previous and end_previous will be set to the start and end of
    the most recent tour for a person.  The first time through,
    start_previous and end_previous are undefined, so make sure to protect
    with a tour_num >= 2 in the variable computation.

    Parameters
    ----------
    tours : DataFrame
        DataFrame of tours containing tour attributes, as well as a person_id
        column to define the nth tour for each person.
    persons_merged : DataFrame
        DataFrame of persons containing attributes referenced by expressions in spec
    alts : DataFrame
        DataFrame of alternatives which represent time slots.  Will be passed to
        interaction_simulate in batches for each nth tour.
    spec : DataFrame
        The spec which will be passed to interaction_simulate.
        (or dict of specs keyed on tour_type if tour_types is not None)

    Returns
    -------
    choices : Series
        A Series of choices where the index is the index of the tours
        DataFrame and the values are the index of the alts DataFrame.
    """

    if not trace_label:
        trace_label = 'vectorize_non_mandatory_tour_scheduling'

    assert len(tours.index) > 0
    assert 'tour_num' in tours.columns
    assert 'tour_type' in tours.columns

    timetable = inject.get_injectable("timetable")
    choice_list = []

    # keep a series of the the most recent tours for each person
    # initialize with first trip from alts
    previous_tour_by_personid = pd.Series(alts.index[0], index=tours.person_id.unique())

    # no more than one tour per person per call to schedule_tours
    # tours must be scheduled in increasing trip_num order
    # second trip of type must be in group immediately following first
    # segregate scheduling by tour_type if multiple specs passed in dict keyed by tour_type

    for tour_num, nth_tours in tours.groupby('tour_num'):

        tour_trace_label = tracing.extend_trace_label(trace_label, 'tour_%s' % (tour_num,))

        if isinstance(spec, dict):

            for tour_type in spec:

                tour_trace_label = tracing.extend_trace_label(trace_label, tour_type)

                choices = \
                    schedule_tours(nth_tours[nth_tours.tour_type == tour_type],
                                   persons_merged, alts,
                                   spec[tour_type],
                                   constants, timetable, previous_tour_by_personid,
                                   chunk_size, tour_trace_label)

                choice_list.append(choices)

        else:

            choices = \
                schedule_tours(nth_tours,
                               persons_merged, alts,
                               spec,
                               constants, timetable, previous_tour_by_personid,
                               chunk_size, tour_trace_label)

            choice_list.append(choices)

    choices = pd.concat(choice_list)

    # add the start, end, and duration from tdd_alts
    tdd = alts.loc[choices]
    tdd.index = choices.index
    # include the index of the choice in the tdd alts table
    tdd['tdd'] = choices

    timetable.replace_table()

    return tdd
