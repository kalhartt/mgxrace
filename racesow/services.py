# methods for performing business logic; reduces complexity of views
import re
import datetime

from djcelery.models import PeriodicTask, IntervalSchedule, TaskState

from racesow.models import Race
from racesow.serializers import raceSerializer
from racesow.utils import average, floats_differ

MIN_REC_POINTS = 2
MAX_REC_POINTS = 100
SECOND_PLACE_PERC = 0.9  # 90% of 1st place
SKIP_BUMPTIME_OFFSET = 20  # number of completed races required for

# define playtime bumps for maps with fewer entries
BUMPTIME_LOW = 600000  # 10 minutes in millis
BUMPTIME_MEDIUM = 1800000  # 30 minutes in millis
BUMPTIME_HIGH = 3600000  # 60 minutes in millis

BUMP_POINTS = {
    BUMPTIME_LOW: 2,
    BUMPTIME_MEDIUM: 5,
    BUMPTIME_HIGH: 10,
}

_playerre = re.compile(r'player($|\(\d*\))', flags=re.IGNORECASE)


def map_get_rec_value(num_completed_races, races, best_race):
    """Compute the number of points to award the 1st player. It considers the amount of playtime other players spent on
     the map, the general idea being: more playtime && more players --> more points for 1st place.

    :param num_completed_races: number of Race objects with a time
    :param races: array of Race objects, sorted by playtime (desc)
    :return: points to award to 1st place (value between MIN_REC_POINTS and MAX_REC_POINTS)
    """

    # determine rec points from playtimes
    rec_points = MIN_REC_POINTS

    # bump rec_points for every player with significant playtime on this map
    for race in races:
        if race.pk == best_race.pk:
            # The fastest player cannot increase his own score by playing longer. On the contrary: the more he
            # plays, the more he bumps the score in case another player takes the rec. As points are always
            # calculated from scratch, the rec will be worth less when he re-recs because his own playtime is once
            # again excluded.
            continue

        if race.playtime > BUMPTIME_HIGH:
            rec_points += BUMP_POINTS[BUMPTIME_HIGH]
        elif race.playtime > BUMPTIME_MEDIUM:
            rec_points += BUMP_POINTS[BUMPTIME_MEDIUM]
        elif race.playtime > BUMPTIME_LOW:
            rec_points += BUMP_POINTS[BUMPTIME_LOW]
        else:
            # no significant Race objects left, break loop
            break

    # check whether we are not exceeding maximum value
    return min(rec_points, MAX_REC_POINTS)


def _update_points(race, new_points, race_rank, reset):
    # save old points value for updating the player totals
    old_points = race.get_points()

    # award points for the player's race
    race.set_points(new_points)
    race.rank = race_rank  # set rank (for medals)
    race.save()

    if reset:
        # race & player points were reset, update player's total points and maps_finished
        race.player.add_points(new_points)
        race.player.maps_finished += 1
    else:
        if not floats_differ(old_points, -1):
            # race did not have points yet, increment finished maps and add points to player total
            race.player.add_points(new_points)
            race.player.maps_finished += 1
        elif floats_differ(new_points, old_points):
            # race did have points, different from the newly computed value; subtract old value from player total
            race.player.add_points(new_points - old_points)
        else:
            # nothing changed for player's total points/maps_finished
            return
    race.player.save()


def map_evaluate_points(mid, reset):
    """Evaluates points awarded to races for map 'mid'.

    :param mid:     map to evaluate races of
    :param reset:   True if all points have been reset,
                    False if points should be updated in-place
    """

    # get Race objects with times (sorted by racetime ascendingly)
    completed_races = Race.objects.filter(map__id=mid, time__isnull=False).order_by('time')
    num_completed_races = len(completed_races)

    if num_completed_races == 0:
        # no races to award points
        return

    # get all Race objects (sorted by playtime descendingly)
    all_races = Race.objects.filter(map__id=mid).order_by('-playtime').select_related('player')

    # determine points for first place
    best_race = completed_races[0]
    rec_points = map_get_rec_value(num_completed_races, all_races, best_race)

    # update race/player points
    _update_points(best_race, rec_points, 1, reset)

    if num_completed_races == 1:
        # no further races to award points
        return

    top20avg = average([race.time for race in completed_races[:20]])  # average of top 20 racetimes

    # cap 2nd place by predefined percentage, with a minimum points difference of 2
    second_place_cap = min(rec_points - 2, rec_points * SECOND_PLACE_PERC)
    x = second_place_cap * (7 / 9)
    first_time = best_race.time

    # compute points for 2nd place (capped to a certain percentage of 1st place)
    second_place_points = min(second_place_cap, second_place_cap -
                              (x * ((completed_races[1].time - first_time) / (top20avg * 0.8))))

    # update race/player points
    _update_points(completed_races[1], second_place_points, 2, reset)

    if num_completed_races == 2:
        # no further races to award points
        return

    # award points for 3rd, 4th... place by differentiating their times with first place, with a minimum of 2
    # points to the previous time
    points_above = second_place_points
    for rank, race in enumerate(completed_races[2:]):
        calculated_points = max(0, min(points_above - 2, second_place_cap -
                                       (x * ((race.time - first_time) / (top20avg * 0.8)))))
        _update_points(race, calculated_points, 3 + rank, reset)
        points_above = calculated_points


def get_record(flt):
    """
    Get the maps serialized record race

    Args:
        flt - A kwarg dict to filter race objects by

    Returns:
        The serialized race dictionary or None if no race exists
    """
    try:
        record = Race.objects.filter(time__isnull=False, **flt).order_by('time')[0]
        return raceSerializer(record)
    except:
        return None


def is_default_username(username):
    # returns True if username is like 'player', 'player(1)' etc.
    return _playerre.match(username)


def get_next_computation_date():
    """
    Returns the next execution of racesow.tasks.recompute_update_maps according to its interval schedule

    :return: None|str
    """
    last_task = TaskState.objects.filter(name='racesow.tasks.recompute_updated_maps').order_by('-tstamp')
    if not last_task:
        return None

    last_task = last_task[0]
    base_time = last_task.tstamp
    soonest = None
    try:
        # find all PeriodicTask entries that contain this task
        for ptask in PeriodicTask.objects.filter(task='racesow.tasks.recompute_updated_maps'):
            try:
                int_sched = IntervalSchedule.objects.get(pk=ptask.interval_id)
                time_delta = datetime.timedelta(**{int_sched.period: int_sched.every})
                next_run = base_time + time_delta

                # remember the soonest execution
                if not soonest or next_run < soonest:
                    soonest = next_run
            except IntervalSchedule.DoesNotExist:
                pass
    except PeriodicTask.DoesNotExist:
        pass
    return soonest
