import re
from collections import defaultdict

import pdfplumber
import pandas as pd

from .days import ALL_DAYS, footnote_days, format_days, is_day_code, parse_day_code
from .geometry import iter_half_pages, rule_ys, cluster, x_mid, rows_overlap

# --- tunables (all in PDF points) ---------------------------------------
BAND_X_TOLERANCE     = 3.0   # merge "RA 5905" / "i 4105" into one word
TRANSFER_X_TOLERANCE = 1.0   # split trailing "s" (togskifte) off its time, e.g. "6.02s" -> "6.02", "s"
COLUMN_TOL           = 6.0   # group x-positions belonging to the same column
ROW_TOL              = 3.0   # group words belonging to the same printed line
MIN_RULE_LEN         = 20    # ignore short horizontal marks

TIME_RE = re.compile(r"^\d{1,2}\.\d{2}$")
# A footnote-reference digit (black square) sits glued to the time it applies to,
# e.g. "❶13.18" extracts as "113.18" unless words are split on font changes.
FOOTNOTE_REF_FONT = "TPLSymboler01"
SPLIT_ON_FONT = ["fontname"]
FOOTNOTE_RE = re.compile(r"^([a-z])\s+(.*)$")
MARK_GAP_MAX = 2.0   # max gap between a time and its trailing transfer mark, in points


# --- locating the Tognummer band ----------------------------------------
def find_tognummer_bands(half, ys, pad=2):
    bands = []
    for w in half.extract_words(x_tolerance=1.5):
        if w["text"].strip().lower() == "tognummer":
            above = [y for y in ys if y <= w["top"] + pad]
            below = [y for y in ys if y >= w["bottom"] - pad]
            if not above or not below:
                continue
            bands.append((max(above), min(below), w))
    return sorted(bands, key=lambda b: b[0])


# --- working out where the columns are ----------------------------------
def centers_from_times(half, body_top, body_bottom):
    """Column positions from the clock times in the table body. These are
    dense (most columns, most rows) so alignment is well-determined."""
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=1.5, extra_attrs=SPLIT_ON_FONT)
    mids = [x_mid(w) for w in words if TIME_RE.match(w["text"].strip())]
    return cluster(mids, COLUMN_TOL)


def centers_from_koredage(half, body_top, body_bottom):
    """Fallback: the operating-days row, one token per column. Scoped to this
    table's body — a half can hold two tables, each with its own "køredage" row."""
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=1.5)
    kd = next((w for w in words if w["text"].strip().lower() == "køredage"), None)
    if kd is None:
        return []
    row = [w for w in words if rows_overlap(w, kd) and w["x0"] > kd["x1"]]
    return cluster([x_mid(w) for w in row], COLUMN_TOL)


# --- reading the band into a matrix -------------------------------------
def band_matrix(half, top, bottom, label_x1, centers):
    """rows (printed lines, top to bottom) x columns -> text"""
    x0, _, x1, _ = half.bbox
    words = [
        w for w in half.within_bbox((x0, top, x1, bottom))
                       .extract_words(x_tolerance=BAND_X_TOLERANCE)
        if w["x0"] > label_x1
    ]
    if not words:
        return []

    line_ys = cluster([w["top"] for w in words], ROW_TOL)
    matrix = [[""] * len(centers) for _ in line_ys]
    for w in words:
        r = min(range(len(line_ys)), key=lambda i: abs(line_ys[i] - w["top"]))
        c = min(range(len(centers)), key=lambda i: abs(centers[i] - x_mid(w)))
        matrix[r][c] = (matrix[r][c] + " " + w["text"]).strip()
    return matrix


STATION_STRIP = re.compile(r"\s+a$")   # trailing arrival marker, e.g. "Fredericia a"


def _is_footnote_noise(text):
    """A footnote reference (digit, or single letter other than the arrival marker
    "a") that sits left of column 1 and would otherwise leak into the station label,
    e.g. "Hjørring a b" (arrival marker "a" + a togskifte-letter footnote "b")."""
    t = text.strip()
    return t.isdigit() or (len(t) == 1 and t.islower() and t != "a")


def footnote_lines(half, table_bottom):
    """Text of each printed line below the table, top to bottom."""
    x0, _, x1, half_bottom = half.bbox
    words = half.within_bbox((x0, table_bottom, x1, half_bottom)).extract_words(x_tolerance=1.5)
    if not words:
        return []

    line_ys = cluster([w["top"] for w in words], ROW_TOL)
    lines = defaultdict(list)
    for w in words:
        r = min(range(len(line_ys)), key=lambda i: abs(line_ys[i] - w["top"]))
        lines[r].append(w)
    return [
        " ".join(w["text"] for w in sorted(lines[r], key=lambda w: w["x0"]))
        for r in sorted(lines)
    ]


def togskifte_footnote_days(lines):
    """{letter: weekdays} for single-letter footnotes (e.g. "a togskifte 5") whose text
    mentions "togskifte" - a day-conditional transfer, printed as a letter suffix on
    a time instead of the plain 's' mark. The transfer only happens on those weekdays
    (all days if the footnote names none, e.g. "udvalgte dage"). Other single-letter
    footnotes exist too (e.g. "vognskifte", a same-train carriage change) and are
    deliberately excluded."""
    letters = {}
    for text in lines:
        m = FOOTNOTE_RE.match(text)
        if m and "togskifte" in m.group(2).lower():
            letters[m.group(1)] = footnote_days(m.group(2)) or ALL_DAYS
    return letters


NUMBERED_FOOTNOTE_RE = re.compile(r"^(\d)\s+(.*)$")
# A wrapped footnote line holds dates (at least one), day codes and these connecting words.
CONTINUATION_RE = re.compile(r"^(?:[\d/,\-\s]|ikke|samt|nat|efter)+$")


def numbered_footnote_days(lines):
    """{digit: weekdays or None} for numbered footnotes (e.g. "1 7, samt 24/12, 31/12")
    referenced under a column's operating days. A footnote can wrap onto lines that
    start with a date or day code ("1 15/12-30/3," + "5/4-13/12 67"). None means the
    footnote only lists dates, so it doesn't restrict weekdays."""
    texts, current = {}, None
    for text in lines:
        m = NUMBERED_FOOTNOTE_RE.match(text)
        if m and m.group(1) not in texts:
            current = m.group(1)
            texts[current] = m.group(2)
        elif current and "/" in text and CONTINUATION_RE.match(text):
            texts[current] += " " + text
        else:
            current = None
    return {digit: footnote_days(text) for digit, text in texts.items()}


def column_days(half, body_top, body_bottom, centers, numbered_footnotes):
    """Weekdays each column runs: the day code in its "køredage" row (all days if
    blank), narrowed by a numbered footnote referenced on the line just below it. A
    column without a day code can instead carry the footnote reference glued to its
    first time (e.g. "❶10.11"); with a day code, such a reference only restricts the
    first stretch of the route, so it's ignored."""
    days = [ALL_DAYS] * len(centers)
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=1.5, extra_attrs=SPLIT_ON_FONT)
    kd = next((w for w in words if w["text"].strip().lower() == "køredage"), None)
    if kd is None:
        return days

    def nearest(w):
        return min(range(len(centers)), key=lambda i: abs(centers[i] - x_mid(w)))

    has_day_code = set()
    for w in words:
        if rows_overlap(w, kd) and w["x0"] > kd["x1"] and is_day_code(w["text"].strip()):
            c = nearest(w)
            days[c] = parse_day_code(w["text"].strip())
            has_day_code.add(c)

    first_times = {}
    for w in words:
        if TIME_RE.match(w["text"].strip()) and w["top"] > kd["bottom"]:
            c = nearest(w)
            if c not in first_times or w["top"] < first_times[c]["top"]:
                first_times[c] = w
    for c, t in first_times.items():
        if c in has_day_code:
            continue
        ref = next(
            (
                w for w in words
                if FOOTNOTE_REF_FONT in w["fontname"] and w["text"].strip().isdigit()
                and rows_overlap(w, t) and 0 <= t["x0"] - w["x1"] <= MARK_GAP_MAX
            ),
            None,
        )
        restriction = numbered_footnotes.get(ref["text"].strip()) if ref else None
        if restriction is not None:
            days[c] = days[c] & restriction

    time_tops = [w["top"] for w in words if TIME_RE.match(w["text"].strip()) and w["top"] > kd["bottom"]]
    first_time_top = min(time_tops) if time_tops else kd["bottom"] + 10
    for w in words:
        text = w["text"].strip()
        if (
            w["top"] >= kd["bottom"] - 1 and w["bottom"] <= first_time_top
            and not rows_overlap(w, kd) and w["x0"] > kd["x1"]
            and text.isdigit() and len(text) == 1
        ):
            restriction = numbered_footnotes.get(text)
            if restriction is not None:
                c = nearest(w)
                days[c] = days[c] & restriction
    return days


BARE_TOGNUMMER_RE = re.compile(r"^[a-z]\s+(\d+)$")


def bare_tognummer(text):
    """Strip the leading category-letter prefix pdfplumber picks up alongside the
    train number (e.g. "i 105" -> "105", "l 13" -> "13"); bare numbers (e.g. "3205")
    pass through unchanged. Used to match against GTFS trip_short_name."""
    t = text.strip()
    m = BARE_TOGNUMMER_RE.match(t)
    return m.group(1) if m else t


STAR_MARK = "★"   # display form of the plain 's' (unconditional togskifte star icon)


def transfer_marks(half, body_top, body_bottom, centers, transfer_letters=frozenset()):
    """[(col_index, station, mark), ...] for every togskifte mark in the body: the
    plain star icon (STAR_MARK) or a footnote letter confirmed by
    togskifte_footnote_letters(), e.g. "a" (look up that page's footnote for the
    day-restriction, since it's otherwise treated the same as an unconditional one)."""
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=TRANSFER_X_TOLERANCE, extra_attrs=SPLIT_ON_FONT)
    if not words:
        return []

    line_ys = cluster([w["top"] for w in words], ROW_TOL)
    rows = defaultdict(list)
    for w in words:
        r = min(range(len(line_ys)), key=lambda i: abs(line_ys[i] - w["top"]))
        rows[r].append(w)

    label_cut = centers[0] - COLUMN_TOL * 2      # everything left of column 1 is the station label
    mark_texts = {"s"} | set(transfer_letters)
    found = []

    for ws in rows.values():
        label_words = sorted(
            (w for w in ws if x_mid(w) < label_cut and not _is_footnote_noise(w["text"])),
            key=lambda w: w["x0"],
        )
        station = STATION_STRIP.sub("", " ".join(w["text"] for w in label_words).strip())

        times = [w for w in ws if TIME_RE.match(w["text"].strip())]
        marks = [w for w in ws if x_mid(w) >= label_cut and w["text"].strip() in mark_texts]

        for m in marks:
            preceding = [t for t in times if t["x1"] <= m["x0"] + 1]
            if not preceding:
                continue
            t = max(preceding, key=lambda t: t["x1"])          # the time this mark belongs to
            if m["x0"] - t["x1"] > MARK_GAP_MAX:                # too far away to be glued to this time
                continue
            c = min(range(len(centers)), key=lambda i: abs(centers[i] - x_mid(t)))
            mark = m["text"].strip()
            found.append((c, station, STAR_MARK if mark == "s" else mark))

    return found

# --- main ---------------------------------------------------------------
def extract(pdf_path):
    records, skipped = [], []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, side, half in iter_half_pages(pdf):
            ys = rule_ys(half)
            if not ys:
                skipped.append((page_num, side, "no horizontal rules"))
                continue

            lines = footnote_lines(half, ys[-1])
            transfer_letters = togskifte_footnote_days(lines)
            numbered_footnotes = numbered_footnote_days(lines)

            prev_bottom = ys[0]
            for top, bottom, label in find_tognummer_bands(half, ys):
                centers = centers_from_times(half, prev_bottom, top) or centers_from_koredage(half, prev_bottom, top)
                if not centers:
                    skipped.append((page_num, side, "no columns found"))
                    prev_bottom = bottom
                    continue

                matrix = band_matrix(half, top, bottom, label["x1"], centers)
                if not matrix:
                    skipped.append((page_num, side, "empty band"))
                    prev_bottom = bottom
                    continue

                days = column_days(half, prev_bottom, top, centers, numbered_footnotes)

                transfers = defaultdict(list)   # col_index -> [(station, mark, weekdays), ...]
                for c, station, mark in transfer_marks(half, prev_bottom, top, centers, transfer_letters):
                    if station and station not in (s for s, _, _ in transfers[c]):
                        transfers[c].append((station, mark, transfer_letters.get(mark, ALL_DAYS)))

                for col_i in range(len(centers)):
                    entries = [row[col_i] for row in matrix]
                    if not any(entries):
                        continue
                    rec = {"page_num": page_num, "side": side, "col_index": col_i}
                    for j, val in enumerate(entries, start=1):
                        if val == "G": #"G" is symbol of "børneguide" (irrelevant)
                            continue
                        rec[f"train_names_{j}"] = val
                    rec["n_train_names"] = sum(1 for val in entries if val)
                    rec["days"] = format_days(days[col_i])
                    rec["has_transfer"] = bool(transfers[col_i])
                    rec["n_transfers"] = len(transfers[col_i])
                    rec["transfer_stations"] = "; ".join(s for s, _, _ in transfers[col_i])
                    rec["transfer_marks"] = "; ".join(m for _, m, _ in transfers[col_i])
                    rec["transfer_days"] = "; ".join(format_days(d) for _, _, d in transfers[col_i])
                    records.append(rec)

                prev_bottom = bottom

    df = pd.DataFrame(records)
    name_cols = sorted(
        [c for c in df.columns if c.startswith("train_names_")],
        key=lambda c: int(c.split("_")[-1]),
    )
    meta = ["n_train_names", "days", "has_transfer", "n_transfers", "transfer_stations", "transfer_marks", "transfer_days"]
    df = df[["page_num", "side", "col_index"] + name_cols + meta].fillna("")
    return df, skipped



if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else ValueError("Missing path to pdf file. Except command-line arguments.")
    df, skipped = extract(path)

