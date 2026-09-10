import hashlib
import pandas as pd


def content_address_block_ids(feed):
    """Replace a feed's block_id numbers with a hash of the trips each block holds.

    Rejseplan renumbers blocks in every release -- ~70% of block numbers shared by
    two consecutive releases describe different routes -- so the raw numbers mean
    different vehicles in different feeds and cannot survive the merge. Hashing the
    block's content instead gives an unchanged block the same id in every release it
    appears in, so its trips still deduplicate across feeds.

    Must run before truncation, so the hash covers the block as published rather than
    whatever part of it survives each feed's date window.
    """
    trips = feed.trips
    if trips is None or "block_id" not in trips.columns:
        return feed

    block = trips["block_id"].astype("string").str.strip()
    has_block = block.notna() & (block != "")
    if not has_block.any():
        return feed

    trips = trips.copy()
    trips["block_id"] = block
    trips = _drop_impossible_blocks(feed, trips)
    block = trips["block_id"]
    has_block = block.notna() & (block != "")
    if not has_block.any():
        return feed

    keys = _trip_keys(feed, trips)

    content = keys[has_block].groupby(trips.loc[has_block, "block_id"]).agg(_hash_keys)
    trips.loc[has_block, "block_id"] = trips.loc[has_block, "block_id"].map(content)
    trips.loc[~has_block, "block_id"] = pd.NA

    feed.trips = trips
    return feed


def _drop_impossible_blocks(feed, trips):
    """Blank block_id on blocks whose trips overlap in time on a day both run.

    One vehicle cannot be in two places at once, so such a block describes no real
    vehicle -- Rejseplan ships a few (e.g. block 6034 in GTFS_20160107 has two trips
    both departing 07:49). OTP rejects their interlines anyway, and exporting them
    fails the validator's block_trips_with_overlapping_stop_times check.
    """
    span = _trip_time_spans(feed)
    trips = trips.assign(
        _start=trips["trip_id"].map(span["start"]),
        _end=trips["trip_id"].map(span["end"]),
    )
    service_days = _service_days(feed)

    blocked = trips[trips["block_id"].notna() & (trips["block_id"] != "")]
    impossible = [
        block_id
        for block_id, group in blocked.groupby("block_id", sort=False)
        if _overlaps_itself(group.sort_values("_start").to_dict("records"), service_days)
    ]

    if impossible:
        print(f"  Dropping block_id on {len(impossible)} block(s) whose trips overlap in time")
        trips.loc[trips["block_id"].isin(impossible), "block_id"] = pd.NA
    return trips.drop(columns=["_start", "_end"])


def _overlaps_itself(rows, service_days):
    """True if two of the block's trips run at the same time on a day both operate."""
    for i, first in enumerate(rows):
        for second in rows[i + 1:]:
            if second["_start"] >= first["_end"]:
                break  # rows are sorted by start, so no later trip overlaps `first` either
            if service_days.get(first["service_id"], frozenset()) & service_days.get(
                second["service_id"], frozenset()
            ):
                return True
    return False


def _trip_time_spans(feed):
    """First departure and last arrival per trip, in seconds (GTFS times may pass 24h)."""
    st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
    grouped = st.groupby("trip_id")
    return {
        "start": grouped["departure_time"].first().map(_to_seconds),
        "end": grouped["arrival_time"].last().map(_to_seconds),
    }


def _to_seconds(clock):
    if pd.isna(clock):
        return None
    hours, minutes, seconds = str(clock).split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _service_days(feed):
    """service_id -> the set of dates it runs, from calendar plus calendar_dates."""
    days = {}
    cal = feed.calendar
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if cal is not None and all(day in cal.columns for day in weekdays):
        for row in cal.to_dict("records"):
            dates = pd.date_range(str(row["start_date"]), str(row["end_date"]))
            runs = [i for i, day in enumerate(weekdays) if str(row[day]) == "1"]
            days[row["service_id"]] = set(dates[dates.dayofweek.isin(runs)].strftime("%Y%m%d"))

    exceptions = feed.calendar_dates
    if exceptions is not None and not exceptions.empty:
        for row in exceptions.to_dict("records"):
            service = days.setdefault(row["service_id"], set())
            if str(row["exception_type"]) == "1":
                service.add(str(row["date"]))
            else:
                service.discard(str(row["date"]))
    return {service: frozenset(dates) for service, dates in days.items()}


def _hash_keys(keys):
    return hashlib.blake2b("|".join(sorted(keys)).encode(), digest_size=8).hexdigest()


def _trip_keys(feed, trips):
    """Feed-independent identity per trip: the same journey in two releases gets the
    same key. The stop_times signature mirrors the one deduplication.py builds."""
    st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
    row_sig = pd.util.hash_pandas_object(
        st[["stop_id", "arrival_time", "departure_time"]], index=False
    ).astype("uint64")
    pos = st.groupby("trip_id").cumcount().astype("uint64") + 1  # make the hash order-sensitive
    stop_times_sig = (row_sig * pos).groupby(st["trip_id"]).sum()

    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    cal = feed.calendar
    if cal is not None and all(day in cal.columns for day in days):
        weekday = cal.set_index("service_id")[days].astype(str).agg("".join, axis=1)
    else:
        weekday = pd.Series(dtype="object")

    keys = (
        trips["route_id"].astype(str)
        + "|" + trips["trip_id"].map(stop_times_sig).astype(str)
        + "|" + trips["service_id"].map(weekday).fillna("?").astype(str)
    )
    if "direction_id" in trips.columns:
        keys = keys + "|" + trips["direction_id"].astype(str)
    return keys
