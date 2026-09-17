"""Parse a DSB Tognummer PDF's validity window from its filename.

Production filenames follow DSB_YYYYMMDD_YYYYMMDD.pdf -- the first date is when
this timetable edition starts, the second is the last date it's used (DSB
publishes a new edition, with its own train numbering, whenever the timetable
changes -- much like Rejseplan's own periodic GTFS releases).
"""

import re
from datetime import datetime
from pathlib import Path

EDITION_RE = re.compile(r"(\d{8})_(\d{8})")


def parse_edition_dates(pdf_path):
    """Returns (valid_from, valid_to) as date objects, or (None, None) if the
    filename doesn't follow the DSB_YYYYMMDD_YYYYMMDD convention (e.g. the
    anonymized sample files under gtfs_merger/tmp/)."""
    m = EDITION_RE.search(Path(pdf_path).stem)
    if not m:
        return None, None
    valid_from = datetime.strptime(m.group(1), "%Y%m%d").date()
    valid_to = datetime.strptime(m.group(2), "%Y%m%d").date()
    return valid_from, valid_to
