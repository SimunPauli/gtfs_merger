"""
config.py — central configuration for the GTFS feed merging/deduplication pipeline.

Edit GTFS_YEAR and the base directories here; everything else is derived.
Other modules should import from this file instead of hardcoding paths.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Year to build the combined feed for
# ---------------------------------------------------------------------------
GTFS_YEAR = int(os.environ.get("GTFS_YEAR", 2022))

# ---------------------------------------------------------------------------
# Base directories — edit if the mount points / folder layout change
# ---------------------------------------------------------------------------
GTFS_CLEAN_DATA_ROOT = Path("/home/simpal/O/sharing-trans-data/GTFS Data/CLEAN - GTFS DATA")
GTFS_SHARE_ROOT = Path("/home/simpal/O/sharing-trans-data/GTFS Data")
OTP_DATA_ROOT = Path("/home/simpal/trip_choice_pipeline/otp_data")
GTFS_VALIDATOR_JAR = Path(__file__).parent / "gtfs-validator" / "gtfs-validator-7.1.0-cli.jar"

# ---------------------------------------------------------------------------
# Derived paths — shouldn't need editing
# ---------------------------------------------------------------------------
GTFS_DATA_ROOT = GTFS_CLEAN_DATA_ROOT / str(GTFS_YEAR)
GTFS_DATA_ROOT_PREV = GTFS_CLEAN_DATA_ROOT / str(GTFS_YEAR - 1)

TEMP_DIR = OTP_DATA_ROOT / str(GTFS_YEAR) / "tmp"

OTP_OUTPUT_PATH = OTP_DATA_ROOT / str(GTFS_YEAR) / f"GTFS_{GTFS_YEAR}.zip"
GTFS_OUTPUT_PATH = GTFS_SHARE_ROOT / f"GTFS_{GTFS_YEAR}.zip"
VALIDATION_OUTPUT_PATH = OTP_DATA_ROOT / str(GTFS_YEAR) / "validation"

# ---------------------------------------------------------------------------
# GTFS table names
# ---------------------------------------------------------------------------
GTFS_TABLES = [
    "agency", "routes", "stops", "trips", "stop_times",
    "calendar", "calendar_dates", "shapes", "transfers",
]

REQUIRED_FILES = [
    "agency", "stops", "routes", "trips", "stop_times",
    "calendar", "calendar_dates", "transfers",
]  # shapes not required — OTP can route without it

MIN_ROWS = 2  # currently unused, carried over from main.py

TABLES_TO_DEDUPLICATE = ["stops", "shapes", "agency", "routes", "calendar", "trips"]