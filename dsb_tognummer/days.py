"""Weekday codes printed in DSB timetables.

Days are numbered 1 (Monday) to 7 (Sunday) and printed as circled digits, which
pdfplumber extracts as plain digits run together: "1-5" = Mon-Fri, "67" = Sat+Sun,
"1-47" = Mon-Thu + Sun, "3" = Wednesday only.
"""

import re

ALL_DAYS = frozenset(range(1, 8))
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DAY_CODE_RE = re.compile(r"^(?:[1-7](?:-[1-7])?)+$")
DAY_PART_RE = re.compile(r"([1-7])(?:-([1-7]))?")


def is_day_code(token):
    """Digits must strictly increase ("1-47", "67"), so e.g. a page number "44" isn't one."""
    if not DAY_CODE_RE.match(token):
        return False
    digits = [int(d) for d in token if d.isdigit()]
    return all(a < b for a, b in zip(digits, digits[1:]))


def parse_day_code(token):
    """ "1-467" -> {1, 2, 3, 4, 6, 7}"""
    days = set()
    for m in DAY_PART_RE.finditer(token):
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first
        days.update(range(first, last + 1))
    return frozenset(days)


def footnote_days(text):
    """Weekdays a footnote restricts to, or None if it has no weekday restriction.

    Dates are ignored (output is per weekday): "7, samt 24/12, 31/12" -> {7},
    "ikke 24/12, 31/12" -> None. "ikke 6" excludes Saturday; "nat efter 56" (the
    night after Fri and Sat) -> {6, 7}.
    """
    tokens = text.replace(",", " ").split()
    days, excluded = set(), set()
    for i, token in enumerate(tokens):
        if not is_day_code(token):
            continue
        parsed = parse_day_code(token)
        if i >= 2 and tokens[i - 2].lower() == "nat" and tokens[i - 1].lower() == "efter":
            days |= {d % 7 + 1 for d in parsed}
        elif i >= 1 and tokens[i - 1].lower() == "ikke":
            excluded |= parsed
        else:
            days |= parsed
    if not days and not excluded:
        return None
    return frozenset((days or ALL_DAYS) - excluded)


def format_days(days):
    """{1, 2, 3, 4, 5} -> "12345" (compact, sortable form stored in extract()'s output)."""
    return "".join(str(d) for d in sorted(days))
