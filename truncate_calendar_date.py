#Function to trunceate GTFS feed at specified date
#Cutoff  date will not be included
import gtfs_kit as gk

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