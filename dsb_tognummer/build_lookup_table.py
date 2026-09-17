"""Build the full table of DSB stay-seated tognummer pairs from every Tognummer PDF
edition in a directory.

Each edition (DSB_YYYYMMDD_YYYYMMDD.pdf) is processed independently (see
stay_seated_pairs.py) and its rows are tagged with that edition's validity window
(valid_from/valid_to, from editions.py) plus its source file, so a downstream step
can pick the edition covering a trip's service date and write the pairs as GTFS
transfers.txt rows with transfer_type=4.

Usage (from gtfs_merger/):
    python -m dsb_tognummer.build_lookup_table <pdf_dir> [output_csv]
"""

import sys
from pathlib import Path

import pandas as pd

from .editions import parse_edition_dates
from .stay_seated_pairs import AMBIGUOUS_COLUMNS, PAIR_COLUMNS, build_stay_seated_pairs

EDITION_COLUMNS = ["valid_from", "valid_to", "source_file"]


def build_full_table(pdf_dir):
    """Returns (pairs_df, ambiguous_df) across every DSB_*.pdf edition in pdf_dir."""
    pairs_frames = []
    ambiguous_frames = []

    for pdf_path in sorted(Path(pdf_dir).glob("DSB_*.pdf")):
        valid_from, valid_to = parse_edition_dates(pdf_path)
        print(f"\nProcessing edition {pdf_path.name} (valid {valid_from} - {valid_to})")

        pairs_df, ambiguous_df = build_stay_seated_pairs(pdf_path)
        edition = {"valid_from": valid_from, "valid_to": valid_to, "source_file": pdf_path.name}
        pairs_frames.append(pairs_df.assign(**edition))
        ambiguous_frames.append(ambiguous_df.assign(**edition))

    pairs_df = (
        pd.concat(pairs_frames, ignore_index=True)
        if pairs_frames else pd.DataFrame(columns=PAIR_COLUMNS + EDITION_COLUMNS)
    )
    ambiguous_df = (
        pd.concat(ambiguous_frames, ignore_index=True)
        if ambiguous_frames else pd.DataFrame(columns=AMBIGUOUS_COLUMNS + EDITION_COLUMNS)
    )
    return pairs_df, ambiguous_df


if __name__ == "__main__":
    pdf_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("dsb_stay_seated_pairs.csv")

    pairs_df, ambiguous_df = build_full_table(pdf_dir)
    pairs_df.to_csv(out_path, index=False)

    print(f"\nWrote {len(pairs_df)} pair(s) across {pairs_df['source_file'].nunique()} edition(s) to {out_path}")
    print(pairs_df["kind"].value_counts().to_string())
    print(f"{len(ambiguous_df)} row(s) left ambiguous across all editions")
