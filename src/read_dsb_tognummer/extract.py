import re
from collections import defaultdict

import pdfplumber
import pandas as pd

from geometry import iter_half_pages, rule_ys, cluster, x_mid, rows_overlap

PDF_PATH = "/home/simpal/trip_choice_pipeline/gtfs_merger/tmp/dsb-k16--icoglyn-.pdf"

# --- tunables (all in PDF points) ---------------------------------------
BAND_X_TOLERANCE     = 3.0   # merge "RA 5905" / "i 4105" into one word
TRANSFER_X_TOLERANCE = 1.0   # split trailing "s" (togskifte) off its time, e.g. "6.02s" -> "6.02", "s"
COLUMN_TOL           = 6.0   # group x-positions belonging to the same column
ROW_TOL              = 3.0   # group words belonging to the same printed line
MIN_RULE_LEN         = 20    # ignore short horizontal marks

TIME_RE = re.compile(r"^\d{1,2}\.\d{2}$")
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
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=1.5)
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


def togskifte_footnote_letters(half, table_bottom):
    """Single-letter footnotes below the table (e.g. "a togskifte 1-5") whose text
    mentions "togskifte" - a day-conditional transfer, printed as a letter suffix on
    a time instead of the plain 's' mark. Other single-letter footnotes exist too
    (e.g. "vognskifte", a same-train carriage change) and are deliberately excluded."""
    x0, _, x1, half_bottom = half.bbox
    words = half.within_bbox((x0, table_bottom, x1, half_bottom)).extract_words(x_tolerance=1.5)
    if not words:
        return set()

    line_ys = cluster([w["top"] for w in words], ROW_TOL)
    lines = defaultdict(list)
    for w in words:
        r = min(range(len(line_ys)), key=lambda i: abs(line_ys[i] - w["top"]))
        lines[r].append(w)

    letters = set()
    for ws in lines.values():
        text = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))
        m = FOOTNOTE_RE.match(text)
        if m and "togskifte" in m.group(2).lower():
            letters.add(m.group(1))
    return letters


STAR_MARK = "★"   # display form of the plain 's' (unconditional togskifte star icon)


def transfer_marks(half, body_top, body_bottom, centers, transfer_letters=frozenset()):
    """[(col_index, station, mark), ...] for every togskifte mark in the body: the
    plain star icon (STAR_MARK) or a footnote letter confirmed by
    togskifte_footnote_letters(), e.g. "a" (look up that page's footnote for the
    day-restriction, since it's otherwise treated the same as an unconditional one)."""
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=TRANSFER_X_TOLERANCE)
    if not words:
        return []

    line_ys = cluster([w["top"] for w in words], ROW_TOL)
    rows = defaultdict(list)
    for w in words:
        r = min(range(len(line_ys)), key=lambda i: abs(line_ys[i] - w["top"]))
        rows[r].append(w)

    label_cut = centers[0] - COLUMN_TOL * 2      # everything left of column 1 is the station label
    mark_texts = {"s"} | transfer_letters
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
def extract(pdf_path=PDF_PATH):
    records, skipped = [], []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, side, half in iter_half_pages(pdf):
            ys = rule_ys(half)
            if not ys:
                skipped.append((page_num, side, "no horizontal rules"))
                continue

            transfer_letters = togskifte_footnote_letters(half, ys[-1])

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

                transfers = defaultdict(list)   # col_index -> [(station, mark), ...]
                for c, station, mark in transfer_marks(half, prev_bottom, top, centers, transfer_letters):
                    if station and station not in (s for s, _ in transfers[c]):
                        transfers[c].append((station, mark))

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
                    rec["has_transfer"] = bool(transfers[col_i])
                    rec["n_transfers"] = len(transfers[col_i])
                    rec["transfer_stations"] = "; ".join(s for s, _ in transfers[col_i])
                    rec["transfer_marks"] = "; ".join(m for _, m in transfers[col_i])
                    records.append(rec)

                prev_bottom = bottom

    df = pd.DataFrame(records)
    name_cols = sorted(
        [c for c in df.columns if c.startswith("train_names_")],
        key=lambda c: int(c.split("_")[-1]),
    )
    meta = ["n_train_names", "has_transfer", "n_transfers", "transfer_stations", "transfer_marks"]
    df = df[["page_num", "side", "col_index"] + name_cols + meta].fillna("")
    return df, skipped



if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else PDF_PATH
    df, skipped = extract(path)
    print(df.head(20))
    print(f"\n{len(skipped)} halves skipped:", skipped[:10])
