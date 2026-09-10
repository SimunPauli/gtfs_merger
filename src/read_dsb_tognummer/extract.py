import re
from collections import defaultdict

import pdfplumber
import pandas as pd

from geometry import iter_half_pages, rule_ys, cluster, x_mid, rows_overlap

PDF_PATH = "tmp/dsb-k25--icoglyn-.pdf"

# --- tunables (all in PDF points) ---------------------------------------
BAND_X_TOLERANCE     = 3.0   # merge "RA 5905" / "i 4105" into one word
TRANSFER_X_TOLERANCE = 1.0   # split trailing "s" (togskifte) off its time, e.g. "6.02s" -> "6.02", "s"
COLUMN_TOL           = 6.0   # group x-positions belonging to the same column
ROW_TOL              = 3.0   # group words belonging to the same printed line
MIN_RULE_LEN         = 20    # ignore short horizontal marks

TIME_RE = re.compile(r"^\d{1,2}\.\d{2}$")


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
    return bands


# --- working out where the columns are ----------------------------------
def centers_from_times(half, body_top, body_bottom):
    """Column positions from the clock times in the table body. These are
    dense (most columns, most rows) so alignment is well-determined."""
    x0, _, x1, _ = half.bbox
    words = half.within_bbox((x0, body_top, x1, body_bottom)).extract_words(x_tolerance=1.5)
    mids = [x_mid(w) for w in words if TIME_RE.match(w["text"].strip())]
    return cluster(mids, COLUMN_TOL)


def centers_from_koredage(half):
    """Fallback: the operating-days row, one token per column."""
    words = half.extract_words(x_tolerance=1.5)
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


def transfer_marks(half, body_top, body_bottom, centers):
    """[(col_index, station), ...] for every 's' (togskifte) in the body."""
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
    found = []

    for ws in rows.values():
        label_words = sorted((w for w in ws if x_mid(w) < label_cut), key=lambda w: w["x0"])
        station = STATION_STRIP.sub("", " ".join(w["text"] for w in label_words).strip())

        times = [w for w in ws if TIME_RE.match(w["text"].strip())]
        marks = [w for w in ws if w["text"].strip() == "s"]

        for m in marks:
            preceding = [t for t in times if t["x1"] <= m["x0"] + 1]
            if not preceding:
                continue
            t = max(preceding, key=lambda t: t["x1"])          # the time this 's' belongs to
            c = min(range(len(centers)), key=lambda i: abs(centers[i] - x_mid(t)))
            found.append((c, station))

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

            for top, bottom, label in find_tognummer_bands(half, ys):
                centers = centers_from_times(half, ys[0], top) or centers_from_koredage(half)
                if not centers:
                    skipped.append((page_num, side, "no columns found"))
                    continue

                matrix = band_matrix(half, top, bottom, label["x1"], centers)
                if not matrix:
                    skipped.append((page_num, side, "empty band"))
                    continue

                transfers = defaultdict(list)
                for c, station in transfer_marks(half, ys[0], top, centers):
                    if station and station not in transfers[c]:
                        transfers[c].append(station)

                for col_i in range(len(centers)):
                    entries = [row[col_i] for row in matrix]
                    if not any(entries):
                        continue
                    rec = {"page_num": page_num, "side": side, "col_index": col_i}
                    for j, val in enumerate(entries, start=1):
                        rec[f"train_names_{j}"] = val
                    rec["n_train_names"] = sum(1 for val in entries if val)
                    rec["has_transfer"] = bool(transfers[col_i])
                    rec["n_transfers"] = len(transfers[col_i])
                    rec["transfer_stations"] = "; ".join(transfers[col_i])
                    records.append(rec)

    df = pd.DataFrame(records)
    name_cols = sorted(
        [c for c in df.columns if c.startswith("train_names_")],
        key=lambda c: int(c.split("_")[-1]),
    )
    meta = ["n_train_names", "has_transfer", "n_transfers", "transfer_stations"]
    df = df[["page_num", "side", "col_index"] + name_cols + meta].fillna("")
    return df, skipped


if __name__ == "__main__":
    df, skipped = extract()
    print(df.head(20))
    print(f"\n{len(skipped)} halves skipped:", skipped[:10])
