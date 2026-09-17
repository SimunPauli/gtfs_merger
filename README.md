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
- `DSB_PAIRS_PATH` — the DSB stay-seated pairs table (`gtfs_merger/dsb_stay_seated_pairs.csv`),
  rebuilt from the Tognummer PDFs whenever they change; see
  [DSB stay-seated transfers](#dsb-stay-seated-transfers)

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
   CSV parser downstream), and drop any table that ended up empty. Before that,
   DSB stay-seated transfers are added to `transfers.txt` as `transfer_type=4` rows
   (see [DSB stay-seated transfers](#dsb-stay-seated-transfers)), with a per-pair
   report at `OTP_DATA_ROOT/<year>/dsb_transfers_report.csv`.
9. **Export** the combined feed to both `GTFS_SHARE_ROOT/GTFS_<year>.zip` (general
   sharing) and `OTP_DATA_ROOT/<year>/GTFS_<year>.zip` (consumed by OTP).
10. **Validate** the exported feed with MobilityData's GTFS Validator CLI
    (`validate_feeds.py`). The run fails if any ERROR-severity notices remain
    (`invalid_url` is ignored — Rejseplan ships enough malformed `agency_url`
    values that it's pure noise); the full report is written under
    `OTP_DATA_ROOT/<year>/validation/`.

---

## DSB stay-seated transfers

DSB trains often change train number (tognummer) without passengers leaving their
seat: a train is renumbered, or wagon sets are coupled or split at a station (e.g.
914 from Sønderborg is coupled onto 14 at Fredericia). Rejseplan's GTFS has no
`block_id` for DSB, and `block_id` can't express coupling/splitting anyway, so these
joins are written as `transfers.txt` rows with `transfer_type=4` (in-seat transfer)
between the two trips, with `from_stop_id`/`to_stop_id` set to the joining station.

It's a two-step process:

1. **PDF → pairs** (`dsb_tognummer/build_lookup_table.py`, run by hand when the PDFs
   change). Parses DSB's yearly Tognummer timetable PDFs (`DSB_YYYYMMDD_YYYYMMDD.pdf`:
   first date = edition start, second = last day used) into
   `dsb_stay_seated_pairs.csv`: one row per `from_tognummer → to_tognummer` pair per
   edition, with its `kind` (renumbering/coupling/splitting), the edition's
   `valid_from`/`valid_to`, and one True/False column per weekday saying on which
   weekdays the PDF has the pair as stay-seated (not a togskifte/transfer).
   ```bash
   python -m dsb_tognummer.build_lookup_table DSB_timetable_pdf dsb_stay_seated_pairs.csv
   ```
2. **Pairs → transfers** (`dsb_tognummer/gtfs_transfers.py`, run by `main.py` before
   export, or standalone on an already merged zip). The PDF only gives train
   numbers and weekdays; the merged feed decides which trips and dates, one date at a
   time. Train numbers are matched on the last 4 digits of `trip_short_name` (DSB
   pads them, e.g. `070014` = train 14), for DSB and Arriva/GoCollective rail trips.
   ```bash
   python -m dsb_tognummer.gtfs_transfers dsb_stay_seated_pairs.csv <merged_zip> <output_zip>
   ```

**Adding transfers to an already merged feed** (no re-merge needed), from `gtfs_merger/`:

```bash
mkdir -p ../otp_data/2024/with_dsb   # a separate folder: output must differ from input
python -m dsb_tognummer.gtfs_transfers dsb_stay_seated_pairs.csv \
    ../otp_data/2024/GTFS_2024.zip ../otp_data/2024/with_dsb/GTFS_2024.zip
GTFS_YEAR=2024 python src/validate_feeds.py ../otp_data/2024/with_dsb ../otp_data/2024/with_dsb/validation
```

(`../otp_data` is the repo's `otp_data`, i.e. `OTP_DATA_ROOT`.) The validator checks every zip in the folder
and needs `GTFS_YEAR` set only because it imports `config.py`. Expect no new errors,
only `transfer_with_suspicious_mid_trip_in_seat` warnings for couplings/splits. If it
looks right, move the new zip over the original and rebuild that year's OTP graph.

**Terms used in the code and report:**

- **Marked date** — a date on which the PDFs say the pair is stay-seated: it lies
  within the validity window of an edition listing the pair, *and* that edition's
  weekday column for the pair is True. Example: 914→14 listed in edition 2024–25
  with Monday–Friday True — Wednesday 12 March 2025 is marked; Saturday 15 March
  2025 is unmarked (weekday False), and so is any date in an edition that doesn't
  list 914→14.
- **Connects** — on a given date, a trip carrying the from-number arrives at a
  station that a trip carrying the to-number departs from 0–60 minutes later, and
  that station ends the from-trip or starts the to-trip. Each from-trip takes its
  closest to-trip, then each to-trip its closest from-trip.
- **Mixed dates** — a `transfers.txt` row has no dates: it links one trip to another
  and holds on every date both trips run. A trip pair has mixed dates when it
  connects on some marked *and* some unmarked dates, e.g. a 14 trip running
  Monday–Saturday where the PDF says stay-seated Monday–Friday but a togskifte on
  Saturday. Such a trip pair is written if it connects on more marked than unmarked
  dates (so OTP also allows staying seated on those unmarked dates) and dropped
  otherwise (so its few marked dates get no in-seat transfer). Both counts are
  printed during the run.

**Report** (`dsb_transfers_report.csv`), one row per pair per edition:
`marked_days` (marked dates within the feed), `connected_days` (marked dates on which
the pair connects), `trip_pairs_written` (`transfers.txt` rows for it). A pair with
no rows typically means its two numbers never run on the same day (the PDF stacks
alternative numbers, e.g. 13 on weekdays / 3213 at weekends), never meet, or run
under different numbers in GTFS.

**Known limits:**

- Trains running under temporary numbers during planned works (e.g. 40 as 340 or
  1040) aren't matched, so those dates get no rows — the main reason coverage is
  low in 2017–2020.
- Weekday restrictions printed partway down a PDF column, and footnote date ranges,
  aren't parsed; GTFS decides the dates instead.
- The 1–2 weeks of the next edition in December's GTFS are matched against that
  edition's pairs.
- In-seat transfers not at the end of the from-trip or the start of the to-trip
  (couplings/splits) give the GTFS Validator's
  `transfer_with_suspicious_mid_trip_in_seat` warning; OTP supports them.

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
| `dsb_tognummer/` | Sibling to `src/`: parses DSB's Tognummer timetable PDFs into stay-seated tognummer pairs (`build_lookup_table.py`, run by hand), and writes them as `transfer_type=4` rows into the merged feed (`gtfs_transfers.py`, called from `main.py`); see [DSB stay-seated transfers](#dsb-stay-seated-transfers). PDF parsing is exercised by `test_pdfplumber_tognummer.ipynb` |

`src/prefix_conflicting_ids.py` and `ID_configuration.py`'s `ID_CONFIG_service_id`
are earlier, unused versions of `prefix_ids.py`/`ID_CONFIG` kept around but not
called from `main.py`'s current pipeline.
