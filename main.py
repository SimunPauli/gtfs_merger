import pandas as pd
import gtfs_kit as gk
import copy
from pathlib import Path
import re
from prefix_ids import apply_prefix_to_all_ids
from id_configuration import ID_CONFIG

gtfs_year = 2025

gtfs_data_root = Path("/home/simpal/O/sharing-trans-data/GTFS Data/CLEAN - GTFS DATA/" + str(gtfs_year))

otp_output_path = Path("/home/simpal/otp/data/gtfs_data/GTFS_" + str(gtfs_year) + ".zip")
gtfs_output_path = Path("/home/simpal/O/sharing-trans-data/GTFS Data/GTFS_" + str(gtfs_year) + ".zip")

if not gtfs_data_root.is_dir():
    print("INPUT ERROR: Directory not found: " + str(gtfs_data_root))
# Recursively find zip files
gtfs_files = list(gtfs_data_root.rglob("*.zip"))

# Extract YYYYMMDD from filename
def extract_date(path):
    match = re.search(r"\d{8}", path.name)
    return pd.to_datetime(match.group(), format="%Y%m%d") if match else None

# Also get the last file from previous year if it exists
gtfs_data_root_prev = Path("/home/simpal/O/sharing-trans-data/GTFS Data/CLEAN - GTFS DATA/" + str(gtfs_year - 1))
if gtfs_data_root_prev.is_dir():
    gtfs_files_prev = list(gtfs_data_root_prev.rglob("*.zip"))
    if gtfs_files_prev:
        gtfs_files_prev_sorted = sorted(gtfs_files_prev, key=extract_date)
        last_prev_file = gtfs_files_prev_sorted[-1]
        gtfs_files.insert(0, last_prev_file)
        print(f"Added last file from {gtfs_year - 1}: {last_prev_file.name}")



gtfs_release = (
    pd.DataFrame({
        "path": gtfs_files,
        "file": [p.name for p in gtfs_files],
        "date": [extract_date(p) for p in gtfs_files],
    })
    .dropna(subset=["date"])
    .sort_values("date")
    .reset_index(drop=True)
    .assign(
        date_end = lambda d: d["date"].shift(-1),
        date_days = lambda d: (d["date"] - pd.Timestamp("1970-01-01")).dt.days,
        date_end_days = lambda d: (d["date_end"] - pd.Timestamp("1970-01-01")).dt.days,
    )
)


print(f"Total number GTFS files: {len(gtfs_release)}")
#
gtfs_list = {}
#Read all gtfs_files
for _, row in gtfs_release.iterrows():
    print(f"Reading GTFS file: {row['file']}")

    feed = gk.feed.read_feed(row["path"], dist_units = "m") #Not sure what dist_unit is.

    gtfs_list[row["file"]] = feed

#
#truncating all files
from truncate_calendar_date import truncate_feed_to_date
for i, row in gtfs_release.iterrows():
    file_key = row["file"]
    cutoff = row["date_end"]

    if pd.isna(cutoff):
        continue

    print(f"Truncating {file_key} to {cutoff.date()}")
    gtfs_list[file_key] = truncate_feed_to_date(gtfs_list[file_key], cutoff)

#
#Merge all separate feeds into one
print("\nMerging all feeds into combined GTFS feed...")

feed_names = list(gtfs_list.keys())
combined_feed = copy.deepcopy(gtfs_list[feed_names[0]])
print(f"Starting with base feed: {feed_names[0]}")

# Add feed_id to the initial feed tables (the same way you do for the following feeds)
for table in [
    "agency", "routes", "stops", "trips", "stop_times",
    "calendar", "calendar_dates", "shapes", "transfers"
]:
    df = getattr(combined_feed, table, None)
    setattr(combined_feed, table, df.assign(feed_id=feed_names[0]))

for feed_name in feed_names[1:]:
    print(f"Merging feed: {feed_name}")
    feed_to_merge = gtfs_list[feed_name]
    #Files are not check for None. All files are required.
    combined_feed.agency = pd.concat([combined_feed.agency, feed_to_merge.agency.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.routes = pd.concat([combined_feed.routes, feed_to_merge.routes.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.stops = pd.concat([combined_feed.stops, feed_to_merge.stops.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.trips = pd.concat([combined_feed.trips, feed_to_merge.trips.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.stop_times = pd.concat([combined_feed.stop_times, feed_to_merge.stop_times.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.calendar = pd.concat([combined_feed.calendar, feed_to_merge.calendar.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.calendar_dates = pd.concat([combined_feed.calendar_dates, feed_to_merge.calendar_dates.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.shapes = pd.concat([combined_feed.shapes, feed_to_merge.shapes.assign(feed_id = feed_name)], ignore_index=True)
    combined_feed.transfers = pd.concat([combined_feed.transfers, feed_to_merge.transfers.assign(feed_id = feed_name)], ignore_index=True)

    del feed_to_merge


print(f"\nAll feed merged. Combined feed statistics:")
if combined_feed.agency is not None:
    print(f"  Agencies: {len(combined_feed.agency)}")
if combined_feed.routes is not None:
    print(f"  Routes: {len(combined_feed.routes)}")
if combined_feed.stops is not None:
    print(f"  Stops: {len(combined_feed.stops)}")
if combined_feed.trips is not None:
    print(f"  Trips: {len(combined_feed.trips)}")
if combined_feed.stop_times is not None:
    print(f"  Stop times: {len(combined_feed.stop_times)}")
if combined_feed.calendar is not None:
    print(f"  Calendar entries: {len(combined_feed.calendar)}")
if combined_feed.shapes is not None:
    print(f"  Shape points: {len(combined_feed.shapes)}")

print("Now deduplication start with prefixing of conflicting IDs:")
for primary_table, config in ID_CONFIG.items():
    if primary_table in ['stops', 'calendar_dates', 'stop_times']:
        continue
    apply_prefix_to_all_ids(combined_feed, config["id_col"], config["foreign_keys"],
                         primary_table)
    print(f"    Prefixed conflicting {config["id_col"]} in all feeds")


from deduplication import deduplicate_feed

tables_to_deduplicate = ['stops', 'shapes', 'agency', 'routes', 'calendar', 'trips']

print("Starting deduplication process...")
print(f"\nBefore deduplication:")
for table in tables_to_deduplicate:
    if getattr(combined_feed, table, None) is not None:
        print(f"  {table}: {len(getattr(combined_feed, table, None))}")

# Apply deduplication for each ID type
for primary_table, config in ID_CONFIG.items():
    print(f"\nDeduplicating {primary_table}...")

    removed = deduplicate_feed(
        combined_feed,
        config["id_col"],
        primary_table,
        config["identity_cols"],
        config["foreign_keys"]
    )

    df = getattr(combined_feed, primary_table, None)
    if df is not None:
        print(f"  {primary_table}: removed {removed} duplicates, {len(df)} remaining")

print("\n" + "=" * 50)
print("Deduplication complete!")
print(f"\nAfter deduplication:")
for table in tables_to_deduplicate + ['stop_times']:
    df = getattr(combined_feed, table, None)
    if df is not None:
        print(f"  {table}: {len(df)}")

del df

# Remove feed_id from every table in combined_feed if present
for table_name in [
    "agency", "routes", "stops", "trips", "stop_times",
    "calendar", "calendar_dates", "shapes", "transfers"
]:
    df = getattr(combined_feed, table_name, None)
    if df is not None and "feed_id" in df.columns:
        setattr(combined_feed, table_name, df.drop(columns=["feed_id"]))
del df

combined_feed.to_file(gtfs_output_path)
combined_feed.to_file(otp_output_path)
