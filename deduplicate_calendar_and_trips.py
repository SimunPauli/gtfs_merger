import pandas as pd

def deduplicate_calendar_and_trips(feed):
    """Replaces the separate 'calendar' and 'trips' dedup branches.
    Run after stops/shapes/agency/routes are already canonical."""

    day_cols = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

    # 1. per-trip content signature, independent of service_id
    df_st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
    df_st["_row_sig"] = pd.util.hash_pandas_object(
        df_st[["stop_id", "arrival_time", "departure_time"]], index=False
    ).astype("uint64")
    df_st["_pos"] = df_st.groupby("trip_id").cumcount().astype("uint64") + 1
    stop_times_sig = (df_st["_row_sig"] * df_st["_pos"]).groupby(df_st["trip_id"]).sum()

    trips = feed.trips.copy()
    trips["_stop_times_sig"] = trips["trip_id"].map(stop_times_sig)
    trips["trip_content_sig"] = pd.util.hash_pandas_object(
        trips[["route_id", "direction_id", "shape_id", "_stop_times_sig"]], index=False
    )  # headsign still excluded, same as your current trip_sig

    # 2. attach each trip's day pattern + date range from calendar
    cal = feed.calendar.copy()
    cal["day_pattern"] = pd.util.hash_pandas_object(cal[day_cols], index=False)
    cal["start_date"] = pd.to_datetime(cal["start_date"])
    cal["end_date"] = pd.to_datetime(cal["end_date"])

    trips = trips.merge(
        cal[["service_id", "day_pattern", "start_date", "end_date"] + day_cols],
        on="service_id", how="left"
    )

    # 3. per (day_pattern, trip_content_sig): merge each pattern's own occurrences
    #    into maximal contiguous date segments. Same contiguity trick as your
    #    calendar branch, just applied per trip pattern instead of per whole service_id.
    trips = trips.sort_values(["day_pattern", "trip_content_sig", "start_date"])
    prev_end = trips.groupby(["day_pattern", "trip_content_sig"], sort=False)["end_date"].shift(1)
    is_contiguous = trips["start_date"] == prev_end + pd.Timedelta(days=1)
    trips["segment_id"] = (~is_contiguous).groupby(
        [trips["day_pattern"], trips["trip_content_sig"]], sort=False
    ).cumsum()

    seg_range = trips.groupby(
        ["day_pattern", "trip_content_sig", "segment_id"], as_index=False, sort=False
    ).agg(seg_start=("start_date", "min"), seg_end=("end_date", "max"))
    trips = trips.merge(seg_range, on=["day_pattern", "trip_content_sig", "segment_id"], how="left")

    # 4. trips that end up with the SAME (day_pattern, seg_start, seg_end) footprint
    #    can share one service_id, even though trip_content_sig differs (different
    #    routes, same validity window). A trip whose footprint diverges from its
    #    neighbours (the one that changed) gets its own, narrower service_id.
    footprint = ["day_pattern", "seg_start", "seg_end"]
    canonical_service = trips.groupby(footprint, sort=False)["service_id"].min()
    trips = trips.merge(canonical_service.rename("service_id_canonical"), on=footprint, how="left")

    # 5. trips identical in (day_pattern, trip_content_sig, segment) are the same trip
    canonical_trip = trips.groupby(
        ["day_pattern", "trip_content_sig", "segment_id"], sort=False
    )["trip_id"].min()
    trips = trips.merge(
        canonical_trip.rename("trip_id_canonical"),
        on=["day_pattern", "trip_content_sig", "segment_id"], how="left"
    )

    # 6. new calendar: one row per footprint, plus any service_id with zero trips
    #    (orphans have no content to dedupe against, so pass them through as-is)
    new_calendar = (
        trips.drop_duplicates(subset=footprint)
        .assign(
            service_id=lambda d: d["service_id_canonical"],
            start_date=lambda d: d["seg_start"].dt.strftime("%Y%m%d"),
            end_date=lambda d: d["seg_end"].dt.strftime("%Y%m%d"),
        )[["service_id", "start_date", "end_date"] + day_cols]
    )
    has_trips = set(trips["service_id"].dropna().unique())
    orphans = feed.calendar[~feed.calendar["service_id"].isin(has_trips)]
    new_calendar = pd.concat([new_calendar, orphans], ignore_index=True)

    # 7. rewrite trips and stop_times using the per-row canonical ids
    new_trips = (
        trips.assign(service_id=trips["service_id_canonical"], trip_id=trips["trip_id_canonical"])
        .drop_duplicates(subset="trip_id")[feed.trips.columns]
        .drop(columns=["block_id"], errors="ignore")  # same as your current trips branch
        .reset_index(drop=True)
    )

    trip_id_map = trips.drop_duplicates(subset="trip_id").set_index("trip_id")["trip_id_canonical"]
    df_st["trip_id"] = df_st["trip_id"].map(trip_id_map).fillna(df_st["trip_id"])
    new_stop_times = (
        df_st.drop(columns=["_row_sig", "_pos"])
        .sort_values(["trip_id", "stop_sequence"])
        .drop_duplicates(["trip_id", "stop_sequence"])
        .reset_index(drop=True)
    )

    # 8. calendar_dates: an old service_id can split across several new ones, so
    #    re-anchor each exception to whichever new service_id's range covers its date
    old_to_new = trips[["service_id", "service_id_canonical", "seg_start", "seg_end"]].drop_duplicates()
    cd = feed.calendar_dates.copy()
    cd["date"] = pd.to_datetime(cd["date"])
    cd = cd.merge(old_to_new, on="service_id", how="left")
    in_range = cd["seg_start"].notna() & cd["date"].between(cd["seg_start"], cd["seg_end"])
    cd.loc[in_range, "service_id"] = cd.loc[in_range, "service_id_canonical"]
    new_calendar_dates = (
        cd.drop(columns=["service_id_canonical", "seg_start", "seg_end"])
        .assign(date=lambda d: d["date"].dt.strftime("%Y%m%d"))
        .drop_duplicates()  # a split service_id can otherwise fan a row out into copies
    )

    feed.calendar = new_calendar.reset_index(drop=True)
    feed.trips = new_trips
    feed.stop_times = new_stop_times
    feed.calendar_dates = new_calendar_dates.reset_index(drop=True)

    if feed.transfers is not None:
        for col in ["from_trip_id", "to_trip_id"]:
            if col in feed.transfers.columns:
                feed.transfers[col] = feed.transfers[col].map(trip_id_map).fillna(feed.transfers[col])

    return feed