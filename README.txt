# GTFS Temporal Merger
### Merging Rejseplan's overlapping, time-sliced GTFS releases into one feed per year

Rejseplan publishes a new GTFS feed release roughly every 10 days, each one extending
about 90 days into the future. So any single calendar year is described by several
overlapping releases, not one canonical feed. This project merges those releases into
a single combined GTFS feed per year — de-duplicating stops, routes, trips, etc. that
recur unchanged across releases, and re-IDing the ones that don't — for use as the
GTFS input to the modified [OpenTripPlanner](../OpenTripPlanner) instance that
[TU Trip Reproducer](../tu_reconstruct_trips) queries.

---

## Requirements

- Python 3.x, dependencies pinned in `requirements.txt` (`gtfs_kit`, `pandas`,
  `geopandas`, `pdfplumber`, ...)
- Java (any recent JRE) — required only for the bundled GTFS Validator CLI jar under
  `gtfs-validator/`, which validates the merged output at the end of a run
- Read access to the raw Rejseplan release zips, and write access to the merged
  feed's output locations (see Configuration below)

---

## Setup

```bash
cd gtfs_merger
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/main.py
```

---

## Configuration

Unlike `tu_reconstruct_trips`, there is no `config.json` — everything is edited
directly in `src/config.py`. The two things that normally need changing:

- `GTFS_YEAR` — the year to build the combined feed for. Read from the `GTFS_YEAR`
  environment variable if set, otherwise defaults to `2022` in the file itself.
- The `*_ROOT` base directories — edit these if the raw-data mount points or output
  folder layout change:
  - `GTFS_CLEAN_DATA_ROOT` — where raw per-year Rejseplan release zips live
    (`GTFS_CLEAN_DATA_ROOT/<year>/*.zip`, searched recursively)
  - `GTFS_SHARE_ROOT` — where the merged feed is written for general sharing
  - `OTP_DATA_ROOT` — the `otp_data/` working-directory tree; the merged feed is
    also written here, at `OTP_DATA_ROOT/<year>/GTFS_<year>.zip`, which is what
    OTP's `build-config.json` consumes
  - `GTFS_VALIDATOR_JAR` — path to the GTFS Validator CLI jar (bundled under
    `gtfs-validator/`)

Everything else in `config.py` (derived paths, the GTFS table list, which tables get
deduplicated) is derived from those and shouldn't normally need editing.

---

## How it works

For a given `GTFS_YEAR`, `src/main.py` runs the following pipeline:

1. **Discover releases.** Recursively find every `*.zip` under
   `GTFS_CLEAN_DATA_ROOT/<year>`, plus the single most-recent release from
   `<year - 1>`, for coverage at the year boundary. Releases are ranked by the
   *earliest date they actually claim service for*, read straight out of each
   feed's own `calendar.txt`/`calendar_dates.txt` — release filenames are not
   trusted for this, since they've been observed not to match the feed's real
   effective date.
2. **Read and normalize each feed** (via `gtfs_kit`): fill in a uniform empty
   `shapes.txt` for feeds that ship none, collapse the various IANA names agencies
   use for Central European Time down to `Europe/Copenhagen`, and content-address
   `block_id` — Rejseplan renumbers blocks in every release, so blocks are re-keyed
   by a hash of the trips they contain, which lets an unchanged block dedupe
   correctly across releases later. Also validates that every feed's stop
   coordinates are plausible WGS84 degrees (catches releases shipped in a
   projected CRS).
3. **Truncate each feed twice:** first to end the day before the *next* release
   takes effect (so overlapping releases don't describe the same day twice), then
   to `GTFS_YEAR`'s calendar bounds.
4. **Concatenate** all feeds' tables into one combined feed, tagging every row with
   a `feed_id` column.
5. **Resolve and drop parent stations.** `transfers.txt` rows that reference a
   parent station (`location_type=1`) are rewritten to its single child stop where
   that's unambiguous; parent stations and the `parent_station` column are then
   dropped entirely, since the merger doesn't support GTFS's parent/child station
   hierarchy.
6. **Prefix conflicting IDs** (`prefix_ids.py`, driven by `ID_configuration.py`'s
   `ID_CONFIG`) — IDs that collide across feeds but aren't the same entity get
   prefixed with their feed's name, per table, in an order where dependencies
   dedupe correctly (stops before stop_times, agency before routes, shapes/routes/
   calendar before trips).
7. **Deduplicate** (`deduplication.py`) — collapses rows that *are* the same entity
   across releases back down to one, remapping foreign keys to match. Logic is
   table-specific:
   - `stops`: snaps coordinates that drift within ~100m to their mean, collapses
     stop_id variants that only differ by trailing "G"s, then dedupes on
     (base stop_id, lat, lon).
   - `shapes`/`stop_times`: hash each ID's full ordered point/stop sequence and
     canonicalize identical sequences.
   - `trips`: hash on route/service/direction/shape/content-addressed block_id plus
     an order-sensitive hash of its stop_times.
   - `calendar`: hash each service's weekday pattern together with everything that
     runs under it, then merge contiguous date ranges sharing that signature.
   - `agency`/`routes`: generic dedup directly on identity columns.
8. **Finalize**: drop the `feed_id` helper column from every table, strip stray
   embedded double-quotes from `stop_name`/`stop_desc` (observed to confuse OTP's
   CSV parser downstream), and drop any table that ended up empty.
9. **Export** the combined feed to both `GTFS_SHARE_ROOT/GTFS_<year>.zip` (general
   sharing) and `OTP_DATA_ROOT/<year>/GTFS_<year>.zip` (consumed by OTP).
10. **Validate** the exported feed with MobilityData's GTFS Validator CLI
    (`validate_feeds.py`). The run fails if any ERROR-severity notices remain
    (`invalid_url` is ignored — Rejseplan ships enough malformed `agency_url`
    values that it's pure noise); the full report is written under
    `OTP_DATA_ROOT/<year>/validation/`.

---

## Module reference

| File | Role |
|---|---|
| `src/main.py` | Orchestrates the full pipeline described above |
| `src/config.py` | All paths and settings; the only file normally edited per run |
| `src/ID_configuration.py` | `ID_CONFIG` — per-table ID column, identity columns, and foreign keys, used by both prefixing and deduplication |
| `src/prefix_ids.py` | Prefixes cross-feed ID collisions with the owning feed's name |
| `src/deduplication.py` | Collapses same-entity rows across feeds; table-specific logic per above |
| `src/truncate_calendar_date.py` | Truncates a feed to a single date or date range |
| `src/content_address_blocks.py` | Re-keys `block_id` by content hash; drops `block_id` from impossible (self-overlapping) blocks |
| `src/missing_shape_file.py` | Normalizes feeds that ship no `shapes.txt` |
| `src/normalize_timezones.py` | Collapses CET timezone-name aliases to `Europe/Copenhagen` |
| `src/validate_feeds.py` | Wraps the GTFS Validator CLI jar; used both standalone and at the end of `main.py` |
| `src/read_dsb_tognummer/` | Standalone PDF-table extractor for DSB Tognummer (train-number) timetables — not part of the merge pipeline itself; exercised by `test_pdfplumber_tognummer.ipynb` |

`src/prefix_conflicting_ids.py` and `ID_configuration.py`'s `ID_CONFIG_service_id`
are earlier, unused versions of `prefix_ids.py`/`ID_CONFIG` kept around but not
called from `main.py`'s current pipeline.
