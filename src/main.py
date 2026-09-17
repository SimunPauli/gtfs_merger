import pandas as pd
import copy
from pathlib import Path
import zipfile
import csv
import io
import os
import errno
import warnings
import tempfile
import sys
from missing_shape_file import normalize_missing_shapes
from normalize_timezones import normalize_timezones
from content_address_blocks import content_address_block_ids
from prefix_ids import apply_prefix_to_all_ids
from ID_configuration import ID_CONFIG
from validate_feeds import validate_feeds_in_dir
import gtfs_kit as gk
from gtfs_kit import constants as cs
import config
sys.path.append(str(Path(__file__).parent.parent))  # dsb_tognummer/ lives beside src/
from dsb_tognummer.gtfs_transfers import add_stay_seated_transfers
cs.DTYPES.setdefault("transfers", {})
cs.DTYPES["transfers"]["from_route_id"] = "string"
cs.DTYPES["transfers"]["to_route_id"] = "string"
cs.DTYPES["transfers"]["from_trip_id"] = "string"
cs.DTYPES["transfers"]["to_trip_id"] = "string"
cs.DTYPES["transfers"]["min_transfer_time"] = "Int32"

def _ensure_dir(path: Path):
	"""
	mkdir that tolerates EINVAL: on network-backed mounts (rclone/cifs-style), a stale
	VFS cache can make os.path.exists() report a directory missing right before mkdir
	hits the real backend where it already exists, and the errno that surfaces through
	the FUSE layer for "already exists" is often EINVAL rather than EEXIST. If mkdir
	fails with EINVAL by there, treat it as success instead
	of crashing a multi-hour merge run right at the export step.
	"""
	try:
		path.mkdir(parents=True, exist_ok=True)
	except OSError as e:
		if e.errno != errno.EINVAL or not path.is_dir():
			raise

def _validate_stop_coordinates(feed, file_name):
	"""
	GTFS requires stop_lat/stop_lon to be WGS84 decimal degrees. Some DTU releases
	have shipped stops in a projected CRS (e.g. UTM32N/ETRS89) instead, which silently
	produces valid-looking numbers that only fail hours later inside OTP.
	"""
	bad = feed.stops.loc[
		~feed.stops["stop_lat"].between(-90, 90) | ~feed.stops["stop_lon"].between(-180, 180)
	]
	if not bad.empty:
		example = bad.iloc[0]
		raise ValueError(
			f"{file_name}: {len(bad)} stop(s) have stop_lat/stop_lon outside the valid "
			f"WGS84 range (likely a projected CRS, not WGS84 degrees) -- e.g. "
			f"stop_id={example['stop_id']!r} lat={example['stop_lat']} lon={example['stop_lon']}"
		)


def _feed_earliest_service_date(path):
	"""
	Earliest date this GTFS release actually claims service for, read directly
	from its own calendar.txt/calendar_dates.txt -- release filenames are not
	used for this anywhere in this module, since DTU release filenames aren't
	reliable (e.g. a file that looks like it's for 2016-02-03 has a calendar
	that only starts 2016-03-03), which would otherwise cause
	truncate_feed_to_date() to cut off the previous release's valid data
	before this one has anything to replace it with.
	Reads only calendar.txt/calendar_dates.txt (not the full feed) so it's
	cheap enough to call on every candidate file before deciding which ones
	are worth fully loading. Returns None if the feed has no usable calendar
	data at all.
	"""
	candidates = []
	with zipfile.ZipFile(path) as z:
		names = set(z.namelist())
		if "calendar.txt" in names:
			with z.open("calendar.txt") as f:
				reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
				starts = [row["start_date"] for row in reader if row.get("start_date")]
				if starts:
					candidates.append(min(starts))
		if "calendar_dates.txt" in names:
			with z.open("calendar_dates.txt") as f:
				reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
				dates = [row["date"] for row in reader if row.get("date")]
				if dates:
					candidates.append(min(dates))
	if not candidates:
		return None
	return pd.to_datetime(min(candidates), format="%Y%m%d")


def main():
	GTFS_TABLES = config.GTFS_TABLES
	GTFS_YEAR = config.GTFS_YEAR

	TEMP_DIR = config.TEMP_DIR
	TEMP_DIR.mkdir(parents=True, exist_ok=True)
	os.environ["TMPDIR"] = str(TEMP_DIR)
	tempfile.tempdir = str(TEMP_DIR)

	gtfs_data_root = config.GTFS_DATA_ROOT
	otp_output_path = config.OTP_OUTPUT_PATH
	gtfs_output_path = config.GTFS_OUTPUT_PATH
	if not gtfs_data_root.is_dir():
		print("INPUT ERROR: Directory not found: " + str(gtfs_data_root))

	# Recursively find zip files
	gtfs_files = list(gtfs_data_root.rglob("*.zip"))

	# Peek each candidate's own calendar.txt/calendar_dates.txt to rank
	# releases by the date they actually take effect (see
	# _feed_earliest_service_date). This is a cheap read (two small CSVs, not
	# the full feed), so it's fine to do for every candidate up front, before
	# deciding which ones are worth fully loading.
	release_dates = {
		path: _feed_earliest_service_date(path) for path in gtfs_files
	}

	# Of the previous year's releases, keep only the one that's most recently
	# in effect -- it's the one covering the transition into this year.
	gtfs_data_root_prev = config.GTFS_DATA_ROOT_PREV
	if gtfs_data_root_prev.is_dir():
		gtfs_files_prev = list(gtfs_data_root_prev.rglob("*.zip"))
		prev_release_dates = {
			path: _feed_earliest_service_date(path) for path in gtfs_files_prev
		}
		prev_release_dates = {p: d for p, d in prev_release_dates.items() if d is not None}
		if prev_release_dates:
			last_prev_file = max(prev_release_dates, key=prev_release_dates.get)
			gtfs_files.append(last_prev_file)
			release_dates[last_prev_file] = prev_release_dates[last_prev_file]
			print(f"Added last file from {GTFS_YEAR - 1}: {last_prev_file.name} "
			      f"(in effect from {prev_release_dates[last_prev_file].date()})")

	gtfs_release = (
		pd.DataFrame({
			"path": gtfs_files,
			"file": [p.name for p in gtfs_files],
			"date": [release_dates[p] for p in gtfs_files],
		})
		.dropna(subset=["date"])
		.sort_values("date")
		.reset_index(drop=True)
		.assign(date_end=lambda d: d["date"].shift(-1))
	)
	if gtfs_release.empty:
	    raise RuntimeError("No GTFS file has usable calendar data. Check gtfs_data_root.")

	print(f"Total number GTFS files: {len(gtfs_release)}")

	### Read all gtfs feeds ###

	gtfs_list = {}
	for _, row in gtfs_release.iterrows():
		print(f"Reading GTFS file: {row['file']}")
		feed = gk.feed.read_feed(row["path"], dist_units="m") # import feed
		feed = normalize_missing_shapes(feed)
		feed = normalize_timezones(feed)
		feed = content_address_block_ids(feed)  # before truncation: hash the block as published
		_validate_stop_coordinates(feed, row["file"])
		gtfs_list[row["file"]] = feed

	# Truncating all files
	from truncate_calendar_date import truncate_feed_to_date, truncate_feed_to_date_range

	print("\nTruncating all feeds to the date before the next release actually takes effect:")
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

	# Some feeds' transfers.txt reference a parent station (location_type=1)
	# directly as from_stop_id/to_stop_id instead of a routable child stop.
	# Since parent stations are dropped below, resolve those references to
	# their child stop first -- but only where the parent has exactly one
	# child, so we're not guessing at ambiguous multi-platform stations.
	# feed_id+stop_id is used as the key because stop_id is only unique
	# within a feed at this point (prefixing happens later, in dedup).
	if "parent_station" in combined_feed.stops.columns and "location_type" in combined_feed.stops.columns:
		child_stops = combined_feed.stops.loc[
			combined_feed.stops["parent_station"].notna() & (combined_feed.stops["parent_station"] != "")
		]
		children_per_parent = child_stops.groupby(["feed_id", "parent_station"])["stop_id"].agg(list)
		single_child_parents = children_per_parent[children_per_parent.map(len) == 1]
		parent_to_child = {
			key: children[0] for key, children in single_child_parents.items()
		}

		df_trans = combined_feed.transfers
		resolved_count = 0
		for col in ["from_stop_id", "to_stop_id"]:
			keys = list(zip(df_trans["feed_id"], df_trans[col]))
			resolved = pd.Series(keys, index=df_trans.index).map(parent_to_child)
			resolved_count += resolved.notna().sum()
			df_trans[col] = resolved.fillna(df_trans[col])
		combined_feed.transfers = df_trans
		print(f"Resolved {resolved_count} parent-station references in transfers.txt to their child stop")

	#Merger can't handel parent stations (location_type=2)
	#Therefor location_type!=1 are removed and parent_station column is removed.
	if "parent_station" in combined_feed.stops.columns:
		combined_feed.stops = combined_feed.stops.drop(columns="parent_station")
	if "location_type" in combined_feed.stops.columns:
		combined_feed.stops = combined_feed.stops.loc[combined_feed.stops.location_type == 0]
	#TODO: Currently only location_type=1 are included in the merging.

	print(f"\nAll files merged. Now deduplication start with prefixing of conflicting IDs:")
	print(f"\nPre-deduplication feed statistics:")
	for table in GTFS_TABLES:
		df = getattr(combined_feed, table, None)
		if df is not None:
			print(f"  {table}: {len(df)}")

	for primary_table, cfg in ID_CONFIG.items():
		if primary_table in ['stops', 'calendar_dates', 'stop_times']:
			continue
		apply_prefix_to_all_ids(combined_feed, cfg["id_col"], cfg["foreign_keys"], primary_table)
		print(f"    Prefixed conflicting {cfg['id_col']} in all feeds")

	from deduplication import deduplicate_feed

	tables_to_deduplicate = config.TABLES_TO_DEDUPLICATE

	print("Starting deduplication process...")
	print(f"\nBefore deduplication:")
	for table in tables_to_deduplicate:
		df = getattr(combined_feed, table, None)
		if df is not None:
			print(f"  {table}: {len(df)}")

	# Apply deduplication for each ID type
	for primary_table, cfg in ID_CONFIG.items():
		print(f"\nDeduplicating {primary_table}...")
		removed = deduplicate_feed(
			combined_feed,
			cfg["id_col"],
			primary_table,
			cfg["identity_cols"],
			cfg["foreign_keys"]
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

	print("\nAdding DSB stay-seated transfers (transfer_type=4):")
	add_stay_seated_transfers(combined_feed, config.DSB_PAIRS_PATH, config.DSB_TRANSFERS_REPORT_PATH)

	# Strip embedded double-quotes from stop text fields: a doubled-quote CSV escape
	# (e.g. a literal " inside stop_name) has been observed to confuse OTP's CSV
	# parser downstream, corrupting subsequent rows' fields.
	for col in ("stop_name", "stop_desc"):
		if col in combined_feed.stops.columns:
			combined_feed.stops[col] = combined_feed.stops[col].str.replace('"', "", regex=False)

	combined_feed.table_names = [
		table_name
		for table_name in GTFS_TABLES
		if getattr(combined_feed, table_name, None) is not None
		   and not getattr(combined_feed, table_name).empty
	]

	print("\nFinal validation before export:")
	for table_name in combined_feed.table_names:
		df = getattr(combined_feed, table_name)
		print(f"  {table_name}: {len(df)} rows, {len(df.columns)} columns")
		if len(df) == 0:
			print(f"    ⚠️  WARNING: {table_name} is empty!")

	print("\nExporting combined GTFS feed to:")
	print(f"  {gtfs_output_path}")
	_ensure_dir(gtfs_output_path.parent)
	combined_feed.to_file(gtfs_output_path)
	print(f"  {otp_output_path}")
	_ensure_dir(otp_output_path.parent)
	combined_feed.to_file(otp_output_path)

	print("\nExport complete!")

	print("\nValidating merged GTFS feed with the GTFS Validator CLI:")
	validation_results = validate_feeds_in_dir(
		otp_output_path.parent,
		config.VALIDATION_OUTPUT_PATH,
		country_code="dk",
	)
	total_errors = sum(r["errors"] for r in validation_results.values())
	if total_errors:
		raise RuntimeError(
			f"GTFS Validator found {total_errors} error-level notice(s) in the merged feed -- "
			f"see {config.VALIDATION_OUTPUT_PATH} for the full report before using this feed."
		)

if __name__ == "__main__":
	main()