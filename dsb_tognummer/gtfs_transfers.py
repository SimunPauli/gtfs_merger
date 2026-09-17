"""Write DSB stay-seated tognummer pairs into a GTFS feed as transfers.txt rows with
transfer_type=4, letting the feed's own trips decide the dates.

The pairs table (build_lookup_table.py) only says which train numbers join, on which
weekdays, within an edition. GTFS decides the rest, date by date:
  - a from-trip and to-trip carrying the pair's numbers are both active that day,
  - the from-trip arrives at a station the to-trip departs from, 0-60 min later,
    and that station ends the from-trip or starts the to-trip,
  - the date lies in an edition listing the pair with that weekday marked.
A trip pair is written when it connects on more marked dates than unmarked ones (a
transfers.txt row has no dates of its own). Pairs whose trains never run on the same
day, or never meet, produce no rows -- see the report.

Train numbers are matched on the last 4 digits of trip_short_name (DSB pads them with
a prefix, e.g. "070014" = train 14), for DSB and Arriva/GoCollective rail trips only.
Numbers used during planned works (e.g. 40 running as 340 or 1040) aren't matched.

Used by src/main.py on the merged feed before export (add_stay_seated_transfers), or
standalone on an already merged zip (from gtfs_merger/):
    python -m dsb_tognummer.gtfs_transfers <pairs_csv> <gtfs_zip> <output_zip>
"""

import re
import shutil
import sys
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from .days import DAY_NAMES

OPERATORS = {"DSB", "Arriva tog", "GoCollective"}
RAIL_ROUTE_TYPE = "2"
MAX_GAP = 60 * 60  # seconds
STOP_TIMES_CHUNK = 5_000_000
TRANSFER_COLUMNS = [
    "from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time",
    "from_route_id", "to_route_id", "from_trip_id", "to_trip_id",
]
REPORT_COLUMNS = [
    "from_tognummer", "to_tognummer", "source_file",
    "marked_days", "connected_days", "trip_pairs_written",
]
STOP_TIMES_COLUMNS = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]


def _number(name):
    """ "RA 5969" -> "5969"; None if the name has no trailing digits."""
    m = re.search(r"(\d+)$", str(name))
    return str(int(m.group(1))) if m else None


def load_pairs(pairs_csv):
    """(from_number, to_number) -> [(valid_from, valid_to, weekdays, source_file), ...]"""
    df = pd.read_csv(pairs_csv, dtype=str)
    pairs = defaultdict(list)
    for row in df.itertuples():
        a, b = _number(row.from_tognummer), _number(row.to_tognummer)
        if not a or not b or a == b:
            continue
        days = frozenset(i + 1 for i, name in enumerate(DAY_NAMES) if getattr(row, name) == "True")
        pairs[(a, b)].append((
            date.fromisoformat(row.valid_from), date.fromisoformat(row.valid_to), days, row.source_file,
        ))
    return pairs


def _text(df, columns):
    """Columns as plain str, whatever dtypes the feed was read with (gtfs_kit or CSV)."""
    return df[columns].astype(str)


def _rail_trips(feed, numbers):
    """Trips of OPERATORS' rail routes whose train number is in numbers."""
    agency = _text(feed.agency, ["agency_id", "agency_name"])
    routes = _text(feed.routes, ["route_id", "agency_id", "route_type"]).merge(agency, on="agency_id")
    routes = routes[(routes.route_type == RAIL_ROUTE_TYPE) & routes.agency_name.isin(OPERATORS)]
    trips = _text(feed.trips, ["route_id", "service_id", "trip_id", "trip_short_name"])
    trips = trips[trips.route_id.isin(routes.route_id) & trips.trip_short_name.str.fullmatch(r"\d+")].copy()
    trips["number"] = (trips.trip_short_name.astype(int) % 10000).astype(str)
    return trips[trips.number.isin(numbers)]


def _service_dates(feed, service_ids):
    """service_id -> {date, ...} from calendar and calendar_dates."""
    dates = defaultdict(set)
    calendar = _text(feed.calendar, ["service_id", "start_date", "end_date"] + DAY_NAMES)
    for row in calendar[calendar.service_id.isin(service_ids)].itertuples():
        weekdays = [getattr(row, name) == "1" for name in DAY_NAMES]
        d, end = pd.Timestamp(row.start_date).date(), pd.Timestamp(row.end_date).date()
        while d <= end:
            if weekdays[d.weekday()]:
                dates[row.service_id].add(d)
            d += timedelta(days=1)
    if feed.calendar_dates is not None:
        exceptions = _text(feed.calendar_dates, ["service_id", "date", "exception_type"])
        for row in exceptions[exceptions.service_id.isin(service_ids)].itertuples():
            d = pd.Timestamp(row.date).date()
            if row.exception_type == "1":
                dates[row.service_id].add(d)
            else:
                dates[row.service_id].discard(d)
    return dates


def _seconds(times):
    hms = times.str.split(":", expand=True).astype(int)
    return hms[0] * 3600 + hms[1] * 60 + hms[2]


def _trip_stations(feed, trip_ids):
    """trip_id -> {first, last, arr: {station: (seconds, stop_id)}, dep: {...}}, with
    stations keyed by stop_name."""
    st = feed.stop_times
    st = _text(st[st.trip_id.isin(trip_ids)], STOP_TIMES_COLUMNS)
    st = st.merge(_text(feed.stops, ["stop_id", "stop_name"]), on="stop_id", how="left")
    st["arr"], st["dep"] = _seconds(st.arrival_time), _seconds(st.departure_time)
    st["stop_sequence"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "stop_sequence"])

    stations = {}
    for trip_id, g in st.groupby("trip_id", sort=False):
        stations[trip_id] = {
            "first": g.stop_name.iloc[0],
            "last": g.stop_name.iloc[-1],
            "arr": dict(zip(g.stop_name, zip(g.arr, g.stop_id))),
            "dep": dict(zip(g.stop_name, zip(g.dep, g.stop_id))),
        }
    return stations


def _connection(f, t, shift):
    """Shortest (gap, from_stop_id, to_stop_id) where f arrives and t departs within
    MAX_GAP at a station ending f or starting t; None if there's none. shift is added
    to t's times (86400 when t runs on the next service day)."""
    best = None
    for station, (arr, from_stop) in f["arr"].items():
        if station not in t["dep"] or "togbus" in station.lower():
            continue
        if station != f["last"] and station != t["first"]:
            continue
        dep, to_stop = t["dep"][station]
        gap = dep + shift - arr
        if 0 <= gap <= MAX_GAP and (best is None or gap < best[0]):
            best = (gap, from_stop, to_stop)
    return best


def build_transfers(feed, pairs):
    """Returns (transfers_df, report_df) for a feed (anything with agency, routes, trips,
    stop_times, stops, calendar, calendar_dates tables) and the pairs from load_pairs()."""
    numbers = {n for pair in pairs for n in pair}
    trips = _rail_trips(feed, numbers)
    print(f"  {len(trips)} rail trip(s) carry a paired train number")
    dates = _service_dates(feed, set(trips.service_id))
    stations = _trip_stations(feed, set(trips.trip_id))

    trips_by_number = defaultdict(list)  # number -> [(trip_id, active dates)]
    for row in trips.itertuples():
        if row.trip_id in stations:
            trips_by_number[row.number].append((row.trip_id, dates[row.service_id]))
    feed_dates = sorted(set().union(*dates.values())) if dates else []

    def active(number, d):
        return [trip_id for trip_id, days in trips_by_number[number] if d in days]

    connections = {}
    def connection(f, t, shift):
        key = (f, t, shift)
        if key not in connections:
            connections[key] = _connection(stations[f], stations[t], shift)
        return connections[key]

    trip_pair_days = defaultdict(lambda: [0, 0])  # (f, t, from_stop, to_stop) -> [marked, unmarked]
    trip_pair_edition = defaultdict(set)            # (f, t, from_stop, to_stop) -> {(a, b, source_file)}
    marked_days = defaultdict(int)
    connected_days = defaultdict(int)

    for (a, b), editions in pairs.items():
        for d in feed_dates:
            edition = next(
                (e for e in editions if e[0] <= d <= e[1] and d.isoweekday() in e[2]), None
            )
            if edition:
                marked_days[(a, b, edition[3])] += 1
            froms = active(a, d)
            if not froms:
                continue
            tos = [(t, 0) for t in active(b, d)] + [(t, 86400) for t in active(b, d + timedelta(days=1))]

            # Each from-trip takes its closest to-trip, then each to-trip its closest from-trip.
            chosen = {}
            for f in froms:
                options = [(c, t) for t, shift in tos if t != f and (c := connection(f, t, shift))]
                if not options:
                    continue
                c, t = min(options)
                if t not in chosen or c < chosen[t][0]:
                    chosen[t] = (c, f)

            if chosen and edition:
                connected_days[(a, b, edition[3])] += 1
            for t, ((_, from_stop, to_stop), f) in chosen.items():
                key = (f, t, from_stop, to_stop)
                trip_pair_days[key][0 if edition else 1] += 1
                if edition:
                    trip_pair_edition[key].add((a, b, edition[3]))

    written = {key: days for key, days in trip_pair_days.items() if days[0] > days[1]}
    n_mixed = sum(1 for days in written.values() if days[1])
    n_dropped = sum(1 for key, days in trip_pair_days.items() if key not in written and days[0])
    print(
        f"  {len(written)} trip pair(s) written ({n_mixed} also connect on some unmarked date); "
        f"{n_dropped} dropped for connecting on more unmarked than marked dates"
    )

    trip_pairs_written = defaultdict(int)
    for key in written:
        for edition_key in trip_pair_edition[key]:
            trip_pairs_written[edition_key] += 1

    transfers = pd.DataFrame(
        [
            {"from_stop_id": from_stop, "to_stop_id": to_stop, "transfer_type": 4,
             "from_trip_id": f, "to_trip_id": t}
            for f, t, from_stop, to_stop in written
        ],
        columns=TRANSFER_COLUMNS,
    )
    report = pd.DataFrame(
        [
            {"from_tognummer": a, "to_tognummer": b, "source_file": source_file,
             "marked_days": n, "connected_days": connected_days[(a, b, source_file)],
             "trip_pairs_written": trip_pairs_written[(a, b, source_file)]}
            for (a, b, source_file), n in marked_days.items()
        ],
        columns=REPORT_COLUMNS,
    )
    return transfers, report


def add_stay_seated_transfers(feed, pairs_csv, report_csv):
    """Append the pairs' transfer_type=4 rows to feed.transfers (skipping trip pairs the
    feed already has a transfer for) and write the per-pair report to report_csv."""
    transfers, report = build_transfers(feed, load_pairs(pairs_csv))
    Path(report_csv).parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(report_csv, index=False)

    existing = feed.transfers if feed.transfers is not None else pd.DataFrame(columns=TRANSFER_COLUMNS)
    if {"from_trip_id", "to_trip_id"} <= set(existing.columns):
        known = set(zip(existing.from_trip_id.astype(str), existing.to_trip_id.astype(str)))
        transfers = transfers[[pair not in known for pair in zip(transfers.from_trip_id, transfers.to_trip_id)]]
    # Match existing dtypes, or concat turns e.g. Int32 min_transfer_time into Float64 ("240.0").
    transfers = transfers.astype({c: existing[c].dtype for c in transfers.columns if c in existing.columns})
    feed.transfers = pd.concat([existing, transfers], ignore_index=True)
    print(f"  transfers: {len(existing)} -> {len(feed.transfers)} row(s); report: {report_csv}")


def _read_zip_feed(gtfs_zip, pairs):
    """The tables build_transfers() needs, with stop_times read in chunks and kept only
    for trips that can carry a paired number."""
    read = lambda z, name, **kw: pd.read_csv(z.open(name), dtype=str, keep_default_na=False, **kw)
    with zipfile.ZipFile(gtfs_zip) as z:
        feed = SimpleNamespace(
            agency=read(z, "agency.txt"), routes=read(z, "routes.txt"), trips=read(z, "trips.txt"),
            stops=read(z, "stops.txt"), calendar=read(z, "calendar.txt"),
            calendar_dates=read(z, "calendar_dates.txt") if "calendar_dates.txt" in z.namelist() else None,
            transfers=read(z, "transfers.txt") if "transfers.txt" in z.namelist() else None,
        )
        trip_ids = set(_rail_trips(feed, {n for pair in pairs for n in pair}).trip_id)
        chunks = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=STOP_TIMES_COLUMNS, chunksize=STOP_TIMES_CHUNK)
        feed.stop_times = pd.concat([c[c.trip_id.isin(trip_ids)] for c in chunks], ignore_index=True)
    return feed


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    pairs_csv, gtfs_zip, output_zip = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if gtfs_zip.resolve() == output_zip.resolve():
        sys.exit("output_zip must differ from gtfs_zip")

    feed = _read_zip_feed(gtfs_zip, load_pairs(pairs_csv))
    add_stay_seated_transfers(feed, pairs_csv, output_zip.with_name(output_zip.stem + "_dsb_transfers_report.csv"))

    # Copy every other file as is, with the extended transfers.txt.
    with zipfile.ZipFile(gtfs_zip) as zin, zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename == "transfers.txt":
                continue
            with zin.open(info) as src, zout.open(info.filename, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
        zout.writestr("transfers.txt", feed.transfers.to_csv(index=False))
    print(f"Wrote {output_zip}")
