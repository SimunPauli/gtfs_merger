import pandas as pd
import gtfs_kit as gk
from typing import List, Tuple, Set
import warnings
import numpy as np

def deduplicate_feed(feed: gk.feed.Feed, id_col: str, primary_table: str, identity_cols: List[str],
                     foreign_keys: List[Tuple[str, str]]) -> int:
    df_primary = getattr(feed, primary_table, None)
    if df_primary is None or df_primary.empty:
        raise ValueError(f"Error: {primary_table} not found in feed")

    initial_count = len(df_primary)

    # Filter identity_cols to those present in the dataframe
    use_identity_cols = [c for c in identity_cols if c in df_primary.columns and c != id_col]

    if not use_identity_cols:
        raise ValueError(f"No identity columns found for {primary_table}")

    if primary_table in ["shapes", "stop_times"]:
        if primary_table == "shapes":
            sequence_cols = "shape_pt_sequence"
        if primary_table == "stop_times":
            sequence_cols = "stop_sequence"

        # For sequence tables: create signature from all rows grouped by ID
        df_primary = df_primary.sort_values([id_col, sequence_cols])

        # Create row-level signature by concatenating identity columns
        df_primary['_row_sig'] = pd.util.hash_pandas_object(
            df_primary[use_identity_cols],
            index=False
        )

        # Group and concatenate row signatures into single signature per ID
        signatures = (
            df_primary
            .groupby(id_col, sort=False)["_row_sig"]
            .apply(tuple)
            .map(hash)
            .rename("_signature")
            .reset_index()
        )
        df_primary.drop(columns=["_row_sig"], inplace=True)
        # Map signature to canonical (minimum) ID
        canonical_map = (
            signatures
            .groupby('_signature', sort=False)[id_col]
            .min()
        )

        # Create ID to canonical ID mapping. For foreign key
        id_to_canonical = (
            signatures
            .set_index(id_col)['_signature']
            .map(canonical_map)
        )

        # Get set of canonical IDs
        canonical_ids = canonical_map.values

        # Update primary table: keep only rows with canonical IDs
        df_primary = (
            df_primary[df_primary[id_col].isin(canonical_ids)]
            .reset_index(drop=True)
            .drop_duplicates(subset=use_identity_cols + [id_col], keep="first")
        )
        setattr(feed, primary_table, df_primary)

    elif primary_table == "trips":
        df_st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
        inital_count_st = len(df_st)

        df_st["_row_sig"] = pd.util.hash_pandas_object(
            df_st[["stop_id", "arrival_time", "departure_time"]],
            index=False
        ).astype("uint64")
        df_st["_pos"] = df_st.groupby("trip_id").cumcount().astype("uint64") + 1 #hash is order-insensitive so adding this
        pattern = (
            (df_st["_row_sig"] * df_st["_pos"])
            .groupby(df_st["trip_id"])
            .sum()
        )
        df_st = df_st.drop("_pos")

        df_st = df_st.drop(columns=["_row_sig"])  # save memory
        df_primary["_stop_times_sig"] = df_primary["trip_id"].map(pattern)

        df_primary["trip_sig"] = pd.util.hash_pandas_object(
            df_primary[["route_id", "service_id", "direction_id", "shape_id", "_stop_times_sig"]],
            index=False
        )

        canonical = (
            df_primary
            .groupby("trip_sig", sort=False)["trip_id"]
            .min()
        )
        trip_map = (
            df_primary.set_index("trip_id")["trip_sig"]
            .map(canonical)
        )

        #Should it handel block_id?? Planning on removing block_id

        df_primary["trip_id"] = df_primary["trip_id"].map(trip_map)
        df_st["trip_id"] = df_st["trip_id"].map(trip_map)

        df_st = (
            df_st
            .sort_values(["trip_id", "stop_sequence"])
            .drop_duplicates(["trip_id", "stop_sequence"])
        )
        df_primary = (
            df_primary
            .sort_values("trip_id")
            .drop_duplicates("trip_id")
        )

        feed.trips = df_primary.reset_index(drop=True)
        feed.stop_times = df_st.reset_index(drop=True)

        final_count = len(df_primary)
        duplicates_removed = initial_count - final_count
        print(f"  stop_times: removed {inital_count_st - len(df_st)} duplicates, {len(df_st)} remaining")
        return duplicates_removed # don't run foreign key loop has it has been done manually for trips (and stop_times)

    elif primary_table == "calendar": #Should run after deduplicating of routes, but before trips (and stop_times)
        df_trips = feed.trips.copy()

        # The goal is to shorten stop_times as it is a very large file,
        # while calendar is small file. Therefor stop_times signiture
        # is joined with calendar.
        df_st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
        df_st["_row_sig"] = pd.util.hash_pandas_object(
            df_st[["stop_id", "arrival_time", "departure_time"]],
            index=False
        ).astype("uint64")
        df_st["_pos"] = df_st.groupby("trip_id").cumcount().astype("uint64") + 1
        pattern_st = (
            (df_st["_row_sig"] * df_st["_pos"])
            .groupby(df_st["trip_id"])
            .sum()
        )
        df_st = df_st.drop("_pos")

        df_trips["_stop_times_sig"] = df_trips["trip_id"].map(pattern_st)
        del df_st, pattern_st # save memory

        df_trips['trip_sig'] = pd.util.hash_pandas_object(
            df_trips[["route_id", "direction_id", "_stop_times_sig"]],
            index=False
        )
        pattern_trips = (
            df_trips
            .groupby("service_id", sort=False)["trip_sig"]
            .agg(lambda x: pd.util.hash_pandas_object(x, index=False).sum())
        )

        df_primary["_trips_service_sig"] = df_primary["service_id"].map(pattern_trips)
        del df_trips
        df_primary['_service_sig'] = pd.util.hash_pandas_object(
            df_primary[use_identity_cols + ["_trips_service_sig"]],
            index=False
        )

        df_primary = df_primary.sort_values(["_service_sig", "start_date"])
        df_primary["prev_end"] = df_primary.groupby("_service_sig", sort=False)["end_date"].shift(1)

        df_primary["is_contiguous"] = (
            df_primary["start_date"] == df_primary["prev_end"] + pd.Timedelta(days=1)
        )
        df_primary["segment_id"] = (
            (~df_primary["is_contiguous"])
            .groupby(df_primary["_service_sig"], sort=False)
            .cumsum()
        )
        df_primary["service_id_canonical"] = (
            df_primary.groupby(["_service_sig", "segment_id"], sort=False)["service_id"]
            .transform("min")
        )
        id_to_canonical = (
            df_primary[["service_id", "service_id_canonical"]]
            .drop_duplicates()
            .set_index("service_id")["service_id_canonical"]
        )
        df_primary = (
            df_primary.groupby(["_service_sig", "segment_id"], as_index=False, sort=False)
            .agg({
                "start_date": "min",
                "end_date": "max",
                "service_id": "min",
                "monday": "first",
                "tuesday": "first",
                "wednesday": "first",
                "thursday": "first",
                "friday": "first",
                "saturday": "first",
                "sunday": "first",
            })
            .rename(columns={"service_id_canonical": "service_id"})
        )

        # Map each unique combination of identity cols to canonical (minimum) ID
        df_primary["service_id_canonical"] = (
            df_primary
            .groupby(use_identity_cols + ['_service_sig'], sort=False)["service_id"]
            .transform('min')
        )

        df_primary = (
            df_primary[
                df_primary["service_id"] == df_primary["service_id_canonical"]
                ]
            .drop(columns=["service_id_canonical", "_service_sig"])
            .reset_index(drop=True)
        )
        setattr(feed, primary_table, df_primary)

    elif primary_table == "stops":
        # coordinate of stops sometimes changes a little bit.
        # I've been told they keep stop_id consistent (!) and only change
        # it when it is moved more than 40 m. The "within 40m=stop_id" is
        # not consistent. I set it to ~100m in lat/lon tolerance at 56N.

        lat_threshold = 0.000898
        lon_threshold = 0.00161

        max_lat_delta = (
            df_primary.groupby("stop_id", sort=False)["stop_lat"]
            .transform(lambda x: (x.mean() - x).abs().max())  # stop distance from average coordinate
        )
        max_lon_delta = (
            df_primary.groupby("stop_id", sort=False)["stop_lon"]
            .transform(lambda x: (x.mean() - x).abs().max())
        )
        df_primary["stable_loc"] = (max_lat_delta < lat_threshold) & (
                    max_lon_delta < lon_threshold)  # ~100m (~141m in diagonal movement) at 56N.

        stable_means = (
            df_primary.loc[df_primary["stable_loc"]]
            .groupby("stop_id", as_index=True, sort=False)[["stop_lat", "stop_lon"]]
            .mean()
        )

        # write back means only for stable rows
        stable_mask = df_primary["stable_loc"]
        df_primary.loc[stable_mask, "stop_lat"] = df_primary.loc[stable_mask, "stop_id"].map(stable_means["stop_lat"])
        df_primary.loc[stable_mask, "stop_lon"] = df_primary.loc[stable_mask, "stop_id"].map(stable_means["stop_lon"])

        # warning flags
        if (~df_primary['stable_loc']).any():
            print(
                "Warning:", (~df_primary['stable_loc']).sum(), "stop_id with max delta lat/lon above 40m threshold:\n",
                df_primary.loc[df_primary['stable_loc'], ["stop_id"]].drop_duplicates(),
                "\nDuplicated stop_id with lat/lon differences > 40, will get new stop_id."
            )

        # drop duplicates with same stop_id/_lat/_lon. lat/lon has been average by stop_id if within 100m. Duplicated stop_id with delta lat/lon, will get new stop_id below.

        # prefix needed for non-unique stop_id
        df_primary["stop_id_prefix"] = (
                df_primary["feed_id"].astype(str)
                .str.replace("GTFS_", "", regex=False)
                .str.replace(".zip", "", regex=False)
                + "_"
                + df_primary["stop_id"].astype(str)
        )


        # Can't use canonical_df as with other, as stop_id is not prefix beforehand.
        # Not prefixed because stop_id is more stable across feeds, but within 100m.
        df_primary["stop_id_prefix_canonical"] = (
            df_primary
            .groupby(["stop_id", "stop_lat", "stop_lon"], sort=False)["stop_id_prefix"]
            .transform("min")
        )

        #correct stop_id for stop_times
        df_st = feed.stop_times
        df_st["stop_id_prefix"] = (
                df_st["feed_id"].astype(str)
                .str.replace("GTFS_", "", regex=False)
                .str.replace(".zip", "", regex=False)
                + "_"
                + df_st["stop_id"].astype(str)
        )
        id_to_canonical = (
            df_primary
            .set_index("stop_id_prefix")["stop_id_prefix_canonical"]
        )
        df_st["stop_id"] = df_st["stop_id_prefix"].map(id_to_canonical).fillna(df_st["stop_id"])
        df_st = df_st.drop(columns=["stop_id_prefix"])
        setattr(feed, "stop_times", df_st)

        df_primary["stop_id"] = df_primary["stop_id_prefix_canonical"]
        df_primary = df_primary.drop(columns=["stop_id_prefix_canonical", "stop_id_prefix"])
        df_primary = df_primary.drop_duplicates(subset=use_identity_cols + ["stop_id"], keep="first")
        setattr(feed, primary_table, df_primary)

        final_count = len(df_primary)
        duplicates_removed = initial_count - final_count
        return duplicates_removed


    else: #only routes and agency
        # For simple tables: group by identity columns directly
        # df_primary = df_primary.sort_values(id_col)

        # Map each unique combination of identity cols to canonical (minimum) ID
        canonical_df = (
            df_primary
            .groupby(use_identity_cols, dropna=False, sort=False)[id_col]
            .min()
            .reset_index()
        )

        # Create mapping from all IDs to canonical IDs
        id_to_canonical = (
            df_primary[[id_col] + use_identity_cols]
            .merge(canonical_df, on=use_identity_cols, suffixes=('', '_canonical'))
            .drop_duplicates(subset=[id_col, f'{id_col}_canonical'])
            .set_index(id_col)[f'{id_col}_canonical']
        )  # For foreign key #For foreign key

        # Update primary table: keep only canonical rows
        canonical_ids = canonical_df[id_col]

        df_primary = (
            df_primary[df_primary[id_col].isin(canonical_ids)]
            .reset_index(drop=True)
            .drop_duplicates(subset=use_identity_cols + [id_col], keep="first")
        )
        setattr(feed, primary_table, df_primary)

    # Update all foreign key references
    for fk_table, fk_col in foreign_keys:
        fk_df = getattr(feed, fk_table, None)
        if fk_df is None or fk_col not in fk_df.columns:
            warnings.warn(f"Warning: Foreign key column {fk_col} not found in {fk_table}. Skipping foreign key update.")
            continue

        # Map foreign keys to canonical IDs
        fk_df[fk_col] = fk_df[fk_col].map(id_to_canonical).fillna(fk_df[fk_col])
        setattr(feed, fk_table, fk_df)

    final_count = len(getattr(feed, primary_table))
    duplicates_removed = initial_count - final_count

    return duplicates_removed