#Function to trunceate GTFS feed at specified date
#Cutoff  date will not be included
import gtfs_kit as gk
import numpy as np
import pandas as pd

def truncate_feed_to_date(feed_i, cutoff_date):
    cutoff_str = cutoff_date.strftime("%Y%m%d")

    if feed_i.calendar is not None:
        cal = feed_i.calendar.copy()
        cal = cal[cal.start_date < cutoff_str]
        from datetime import timedelta
        cutoff_exclusive = (cutoff_date - timedelta(days=1)).strftime("%Y%m%d")
        cal.loc[cal.end_date >= cutoff_str, "end_date"] = cutoff_exclusive
        feed_i.calendar = cal
        del cal

    if feed_i.calendar_dates is not None:
        cd = feed_i.calendar_dates.copy()
        cd = cd[cd.date < cutoff_str]
        feed_i.calendar_dates = cd
        del cd

    #filter trips using valid service_id
    valid_service_ids = set()
    if feed_i.calendar is not None:
        valid_service_ids.update(feed_i.calendar.service_id.unique())

    if feed_i.calendar_dates is not None:
        valid_service_ids.update(feed_i.calendar_dates.service_id.unique())

    feed_i.trips = feed_i.trips[
        feed_i.trips.service_id.isin(valid_service_ids)
    ]

    #restrict_to_trips: Build a new feed by restricting this one to only the stops, trips, shapes, etc. used by the trips of the given IDs. Return the resulting feed.
    feed_trunc = gk.miscellany.restrict_to_trips(feed_i, feed_i.trips.trip_id.tolist())


    return feed_trunc

from datetime import timedelta

def truncate_feed_to_date_range(feed_i, start_date, end_date):
    """
    Keep GTFS service only in [start_date, end_date).
    end_date is exclusive.
    """
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")
    end_inclusive_str = (end_date - timedelta(days=1)).strftime("%Y%m%d")

    if feed_i.calendar is not None:
        cal = feed_i.calendar.copy()

        # Keep services overlapping the target range
        cal = cal[
            (cal.end_date >= start_str) &
            (cal.start_date < end_str)
        ]

        # Clip service range to target year
        cal.loc[cal.start_date < start_str, "start_date"] = start_str
        cal.loc[cal.end_date >= end_str, "end_date"] = end_inclusive_str

        feed_i.calendar = cal

    if feed_i.calendar_dates is not None:
        cd = feed_i.calendar_dates.copy()

        cd = cd[
            (cd.date >= start_str) &
            (cd.date < end_str)
        ]

        feed_i.calendar_dates = cd

    valid_service_ids = set()

    if feed_i.calendar is not None:
        valid_service_ids.update(feed_i.calendar.service_id.unique())

    if feed_i.calendar_dates is not None:
        valid_service_ids.update(feed_i.calendar_dates.service_id.unique())

    feed_i.trips = feed_i.trips[
        feed_i.trips.service_id.isin(valid_service_ids)
    ]

    feed_trunc = gk.miscellany.restrict_to_trips(
        feed_i,
        feed_i.trips.trip_id.tolist()
    )

    return feed_trunc


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def active_service_dates(feed_i):
    """Dates on which at least one of the feed's trips runs."""
    used = set(feed_i.trips.service_id)
    active, removed = set(), set()  # (service_id, date) pairs

    if feed_i.calendar is not None and not feed_i.calendar.empty:
        cal = feed_i.calendar[feed_i.calendar.service_id.isin(used)]
        rows = cal.assign(date=[
            pd.date_range(s, e) for s, e in zip(
                pd.to_datetime(cal.start_date, format="%Y%m%d"),
                pd.to_datetime(cal.end_date, format="%Y%m%d"),
            )
        ]).explode("date").dropna(subset=["date"])
        rows["date"] = pd.to_datetime(rows["date"])
        # keep only dates whose weekday flag is set
        runs = rows[WEEKDAYS].to_numpy()[np.arange(len(rows)), rows["date"].dt.dayofweek] == 1
        active.update(zip(rows.service_id[runs], rows.date[runs]))

    if feed_i.calendar_dates is not None and not feed_i.calendar_dates.empty:
        cd = feed_i.calendar_dates[feed_i.calendar_dates.service_id.isin(used)]
        cd = cd.assign(date=pd.to_datetime(cd.date, format="%Y%m%d"))
        added = cd.exception_type == 1
        active.update(zip(cd.service_id[added], cd.date[added]))
        removed.update(zip(cd.service_id[~added], cd.date[~added]))

    return {date for _, date in active - removed}


def report_uncovered_dates(feeds, start_date, end_date):
    """Warn about each run of dates in [start_date, end_date) with no service in any feed."""
    covered = set().union(*(active_service_dates(f) for f in feeds))
    missing = pd.date_range(start_date, end_date, inclusive="left").difference(pd.DatetimeIndex(list(covered)))
    if missing.empty:
        print("  Every date has service")
        return
    missing = missing.to_series()
    run_id = (missing.diff() != pd.Timedelta(days=1)).cumsum()
    print(f"Warning: {len(missing)} date(s) have no service in any feed:")
    for _, run in missing.groupby(run_id):
        print(f"  {run.iloc[0].date()} - {run.iloc[-1].date()} ({len(run)} days)")