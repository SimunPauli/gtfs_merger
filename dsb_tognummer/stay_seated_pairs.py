"""Derive DSB's stay-seated tognummer pairs from a single Tognummer timetable PDF.

Each pair (from_tognummer -> to_tognummer) means passengers can stay on board when
the train number changes, and is meant to become a GTFS transfers.txt row with
transfer_type=4. Pairs are used instead of block_id because some joins are wagon
sets coupling (two trains -> one) or splitting (one -> two), which a linear block
can't represent. Weekday columns (monday..sunday) say on which days the pair holds.

For a given extracted row, train_names_1..N are the stacked train numbers used down
one printed timetable column, in order, and the transfers active on a weekday count
how many of the N-1 joins are passenger transfers (togskifte) that day:
  - 0 transfers               -> every join is stay-seated
  - N - 1 transfers           -> every join is a transfer
  - in between                -> ambiguous from this row alone; resolved where other
                                 rows of the same edition pin down its joins
  - more than N - 1           -> a transfer to a train not listed in the stack (e.g.
                                 another operator's continuation); row left out

Everything is resolved one weekday at a time, using only rows whose column runs that
day. Each PDF edition (DSB_YYYYMMDD_YYYYMMDD.pdf) is processed on its own, with no
comparison against other editions.
"""

from collections import defaultdict

import pandas as pd

from .days import DAY_NAMES
from .extract import extract, bare_tognummer

NAME_COL_PREFIX = "train_names_"
PAIR_COLUMNS = ["from_tognummer", "to_tognummer", "kind", "n_rows"] + DAY_NAMES
AMBIGUOUS_COLUMNS = ["page_num", "side", "col_index", "names", "transfer_stations"] + DAY_NAMES


def _row_names(row, name_cols):
    return [bare_tognummer(row[c]) for c in name_cols if row[c]]


def _parse_days(text):
    return frozenset(int(d) for d in text)


def _row_records(df, name_cols):
    """This edition's rows -> [{joins, days, transfer_days, row_ref}, ...]; joins are
    ordered (earlier name, later name) tuples. Rows with a single name have no join."""
    records = []
    for _, row in df.iterrows():
        names = _row_names(row, name_cols)
        if len(names) < 2:
            continue
        records.append({
            "joins": list(zip(names[:-1], names[1:])),
            "days": _parse_days(row["days"]),
            "transfer_days": [_parse_days(d) for d in row["transfer_days"].split("; ") if d],
            "row_ref": {
                "page_num": row["page_num"],
                "side": row["side"],
                "col_index": row["col_index"],
                "names": tuple(names),
                "transfer_stations": row["transfer_stations"],
            },
        })
    return records


def _resolve_joins(items):
    """Classify every join as stay-seated or transfer for one weekday.
    items: [(row_key, joins, n_transfers), ...]. Returns (same_edges, diff_pairs,
    pending_keys), with edges mapped to the row keys supporting them.

    A row with 0 < n_transfers < N - 1 can often be resolved once other rows have
    classified some of its joins: if (A, B) is already known stay-seated, a row
    (A, B, C) with 1 transfer must have it on (B, C). Repeats until nothing changes.
    Only direct pairs count as known -- not chains through other trains, since a
    coupling point would otherwise link two different wagon sets.
    """
    same_edges = defaultdict(set)   # (from, to) -> {row_key, ...}
    diff_pairs = defaultdict(set)   # frozenset({a, b}) -> {row_key, ...}
    pending = []

    for key, joins, n_transfers in items:
        if n_transfers == 0:
            for join in joins:
                same_edges[join].add(key)
        elif n_transfers == len(joins):
            for join in joins:
                diff_pairs[frozenset(join)].add(key)
        else:
            pending.append((key, joins, n_transfers))

    changed = True
    while changed and pending:
        changed = False
        still_pending = []
        for key, joins, n_transfers in pending:
            unknown = []
            n_diff = 0
            for a, b in joins:
                if (a, b) in same_edges or (b, a) in same_edges:
                    continue
                if frozenset((a, b)) in diff_pairs:
                    n_diff += 1
                else:
                    unknown.append((a, b))

            remaining = n_transfers - n_diff
            if not unknown:
                changed = True
            elif remaining == 0:
                for join in unknown:
                    same_edges[join].add(key)
                changed = True
            elif remaining == len(unknown):
                for join in unknown:
                    diff_pairs[frozenset(join)].add(key)
                changed = True
            else:
                still_pending.append((key, joins, n_transfers))
        pending = still_pending

    return same_edges, diff_pairs, [key for key, _, _ in pending]


def build_stay_seated_pairs(pdf_path):
    """Process a single Tognummer PDF edition. Returns (pairs_df, ambiguous_df).

    pairs_df: [from_tognummer, to_tognummer, kind, n_rows, monday..sunday] -- kind is
    renumbering, coupling (to_tognummer has more than one predecessor on some day),
    splitting (from_tognummer has more than one successor on some day) or both;
    n_rows counts supporting rows; weekday columns are True on days the pair holds.
    ambiguous_df: rows still unresolved after using the whole edition's evidence,
    with weekday columns True on the days they stayed unresolved.
    """
    source_file = str(pdf_path)
    df, _skipped = extract(source_file)
    if df.empty:
        return pd.DataFrame(columns=PAIR_COLUMNS), pd.DataFrame(columns=AMBIGUOUS_COLUMNS)

    name_cols = sorted(
        [c for c in df.columns if c.startswith(NAME_COL_PREFIX)],
        key=lambda c: int(c.split("_")[-1]),
    )
    records = _row_records(df, name_cols)

    pair_days = defaultdict(set)      # (from, to) -> {day, ...}
    pair_rows = defaultdict(set)      # (from, to) -> {row_key, ...}
    pair_kinds = defaultdict(set)     # (from, to) -> {"coupling", "splitting"}
    ambiguous_days = defaultdict(set) # row_key -> {day, ...}
    too_many_days = defaultdict(set)  # row_key -> {day, ...}
    conflict_days = defaultdict(set)  # (from, to) -> {day, ...}

    for day in range(1, 8):
        items = []
        for key, rec in enumerate(records):
            if day not in rec["days"]:
                continue
            n_transfers = sum(1 for d in rec["transfer_days"] if day in d)
            if n_transfers > len(rec["joins"]):
                too_many_days[key].add(day)
                continue
            items.append((key, rec["joins"], n_transfers))

        same_edges, diff_pairs, pending = _resolve_joins(items)
        for key in pending:
            ambiguous_days[key].add(day)

        for edge in list(same_edges):
            if frozenset(edge) in diff_pairs:
                conflict_days[edge].add(day)
                del same_edges[edge]

        predecessors, successors = defaultdict(set), defaultdict(set)
        for from_name, to_name in same_edges:
            predecessors[to_name].add(from_name)
            successors[from_name].add(to_name)

        for edge, keys in same_edges.items():
            pair_days[edge].add(day)
            pair_rows[edge] |= keys
            if len(predecessors[edge[1]]) > 1:
                pair_kinds[edge].add("coupling")
            if len(successors[edge[0]]) > 1:
                pair_kinds[edge].add("splitting")

    for key, days in too_many_days.items():
        print(
            f"Warning: [{source_file}] more transfers than joins on days {sorted(days)} "
            f"(transfer to a train not in the stack) -- leaving out {records[key]['row_ref']}"
        )
    for edge, days in conflict_days.items():
        print(
            f"Warning: [{source_file}] conflicting evidence for tognummer {edge[0]!r}->{edge[1]!r} "
            f"on days {sorted(days)} -- stay-seated in some row(s), a transfer in others. Dropping pair on those days."
        )

    pairs_df = pd.DataFrame(
        [
            {
                "from_tognummer": edge[0],
                "to_tognummer": edge[1],
                "kind": ";".join(k for k in ("coupling", "splitting") if k in pair_kinds[edge]) or "renumbering",
                "n_rows": len(pair_rows[edge]),
                **{name: (i + 1) in days for i, name in enumerate(DAY_NAMES)},
            }
            for edge, days in pair_days.items()
        ],
        columns=PAIR_COLUMNS,
    )
    ambiguous_df = pd.DataFrame(
        [
            {**records[key]["row_ref"], **{name: (i + 1) in days for i, name in enumerate(DAY_NAMES)}}
            for key, days in ambiguous_days.items()
        ],
        columns=AMBIGUOUS_COLUMNS,
    )
    if not ambiguous_df.empty:
        print(f"[{source_file}] {len(ambiguous_df)} row(s) left ambiguous on at least one day")

    return pairs_df, ambiguous_df
