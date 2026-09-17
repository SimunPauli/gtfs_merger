"""Generic pdfplumber geometry helpers: page splitting, rule detection, 1-D clustering."""


def iter_half_pages(pdf, split_x=None):
    """Each PDF page is two printed pages side by side."""
    for page_num, page in enumerate(pdf.pages, start=1):
        x = split_x if split_x is not None else page.width / 2
        yield page_num, "L", page.within_bbox((0, 0, x, page.height))
        yield page_num, "R", page.within_bbox((x, 0, page.width, page.height))


def rule_ys(page, min_len=20):
    edges = getattr(page, "horizontal_edges", None)
    if edges is None:
        edges = [e for e in page.edges if e.get("orientation") == "h"]
    return sorted({round(e["top"], 1) for e in edges if (e["x1"] - e["x0"]) >= min_len})


def cluster(values, tol):
    """1-D clustering: returns the mean of each group of nearby values."""
    values = sorted(values)
    groups = []
    for v in values:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def x_mid(w):
    return (w["x0"] + w["x1"]) / 2


def rows_overlap(w1, w2, min_frac=0.5):
    overlap = max(0, min(w1["bottom"], w2["bottom"]) - max(w1["top"], w2["top"]))
    shortest = min(w1["bottom"] - w1["top"], w2["bottom"] - w2["top"])
    return shortest > 0 and overlap / shortest >= min_frac
