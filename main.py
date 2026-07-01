import pandas as pd
import gtfs_kit as gk
import copy
from pathlib import Path
import re
import os
import tempfile
from prefix_ids import apply_prefix_to_all_ids
from ID_configuration import ID_CONFIG

# Define constant for table names
GTFS_TABLES = [
	"agency", "routes", "stops", "trips", "stop_times",
	"calendar", "calendar_dates", "shapes", "transfers"
]

GTFS_YEAR = 2024

TEMP_DIR = Path("/home/simpal/otp/data/gtfs_data/tmp")
TEMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(TEMP_DIR)
tempfile.tempdir = str(TEMP_DIR)

gtfs_data_root = Path("/home/simpal/O/sharing-trans-data/GTFS Data/CLEAN - GTFS DATA/" + str(GTFS_YEAR))
otp_output_path = Path("/home/simpal/otp/data/gtfs_data/GTFS_" + str(GTFS_YEAR) + ".zip")
gtfs_output_path = Path("/home/simpal/O/sharing-trans-data/GTFS Data/GTFS_" + str(GTFS_YEAR) + ".zip")

if not gtfs_data_root.is_dir():
	print("INPUT ERROR: Directory not found: " + str(gtfs_data_root))

# Recursively find zip files
gtfs_files = list(gtfs_data_root.rglob("*.zip"))


# Extract YYYYMMDD from filename
def extract_date(path):
	match = re.search(r"\d{8}", path.name)
	return pd.to_datetime(match.group(), format="%Y%m%d") if match else None


# Also get the last file from previous year if it exists
gtfs_data_root_prev = Path("/home/simpal/O/sharing-trans-data/GTFS Data/CLEAN - GTFS DATA/" + str(GTFS_YEAR - 1))
if gtfs_data_root_prev.is_dir():
	gtfs_files_prev = list(gtfs_data_root_prev.rglob("*.zip"))
	if gtfs_files_prev:
		# Sort and get the last file from previous year
		gtfs_files_prev_sorted = sorted(gtfs_files_prev, key=extract_date)
		last_prev_file = gtfs_files_prev_sorted[-1]
		gtfs_files.insert(0, last_prev_file)
		print(f"Added last file from {GTFS_YEAR - 1}: {last_prev_file.name}")

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
		date_end=lambda d: d["date"].shift(-1),
		date_days=lambda d: (d["date"] - pd.Timestamp("1970-01-01")).dt.days,
		date_end_days=lambda d: (d["date_end"] - pd.Timestamp("1970-01-01")).dt.days,
	)
)
if gtfs_release.empty:
    raise RuntimeError("No GTFS files found. Check gtfs_data_root.")

print(f"Total number GTFS files: {len(gtfs_release)}")

gtfs_list = {}
# Read all gtfs_files
for _, row in gtfs_release.iterrows():
	print(f"Reading GTFS file: {row['file']}")
	feed = gk.feed.read_feed(row["path"], dist_units="m")
	gtfs_list[row["file"]] = feed

# Truncating all files
from truncate_calendar_date import truncate_feed_to_date, truncate_feed_to_date_range

print("\nTruncating all feeds to the date before next feed release:")
for i, row in gtfs_release.iterrows():
	file_key = row["file"]
	cutoff = row["date_end"]
	if pd.isna(cutoff):
		continue
	print(f"Truncating {file_key} to {cutoff.date()}")
	gtfs_list[file_key] = truncate_feed_to_date(gtfs_list[file_key], cutoff)


year_start = pd.Timestamp(f"{GTFS_YEAR}-01-01")
year_end = pd.Timestamp(f"{GTFS_YEAR + 1}-01-01")
print(f"\nTruncating all feeds to GTFS_YEAR={GTFS_YEAR}:")
for file_key in list(gtfs_list.keys()):
    print(f"Truncating {file_key} to {year_start.date()} - {year_end.date()}")
    gtfs_list[file_key] = truncate_feed_to_date_range(
        gtfs_list[file_key],
        year_start,
        year_end
    )

# Merge all separate feeds into one
print("\nMerging all feeds into combined GTFS feed...")
feed_names = list(gtfs_list.keys())
combined_feed = copy.deepcopy(gtfs_list[feed_names[0]])
print(f"Starting with base feed: {feed_names[0]}")

# Add feed_id to the initial feed tables
for table in GTFS_TABLES:
	df = getattr(combined_feed, table, None)
	if df is not None:
		setattr(combined_feed, table, df.assign(feed_id=feed_names[0]))

for feed_name in feed_names[1:]:
	print(f"Merging feed: {feed_name}")
	feed_to_merge = gtfs_list[feed_name]

	for table in GTFS_TABLES:
		df_base = getattr(combined_feed, table, None)
		df_new = getattr(feed_to_merge, table, None)

		# Only concatenate if both exist
		if df_base is not None and df_new is not None:
			setattr(combined_feed, table,
			        pd.concat([df_base, df_new.assign(feed_id=feed_name)],
			                  ignore_index=True))

	del feed_to_merge

print(f"\nAll files merged. Now deduplication start with prefixing of conflicting IDs:")
print(f"\nPre-deduplication feed statistics:")
for table in GTFS_TABLES:
	df = getattr(combined_feed, table, None)
	if df is not None:
		print(f"  {table}: {len(df)}")

for primary_table, config in ID_CONFIG.items():
	if primary_table in ['stops', 'calendar_dates', 'stop_times']:
		continue
	apply_prefix_to_all_ids(combined_feed, config["id_col"], config["foreign_keys"],
	                        primary_table)
	print(f"    Prefixed conflicting {config['id_col']} in all feeds")

from deduplication import deduplicate_feed

tables_to_deduplicate = ['stops', 'shapes', 'agency', 'routes', 'calendar', 'trips']
print("Starting deduplication process...")
print(f"\nBefore deduplication:")
for table in tables_to_deduplicate:
	df = getattr(combined_feed, table, None)
	if df is not None:
		print(f"  {table}: {len(df)}")

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

# Remove feed_id from every table in combined_feed if present
for table_name in GTFS_TABLES:
	df = getattr(combined_feed, table_name, None)
	if df is not None and "feed_id" in df.columns:
		setattr(combined_feed, table_name, df.drop(columns=["feed_id"]))

combined_feed.to_file(gtfs_output_path)
combined_feed.to_file(otp_output_path)