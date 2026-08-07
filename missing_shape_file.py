import pandas as pd

def normalize_missing_shapes(feed):
    """Make a feed without shapes.txt indistinguishable from a feed
    with an empty (header-only) shapes.txt."""
    if feed.trips is not None and "shape_id" not in feed.trips.columns:
        feed.trips = feed.trips.assign(
            shape_id=pd.array([pd.NA] * len(feed.trips), dtype="string")
        )
    if feed.shapes is None:
        feed.shapes = pd.DataFrame({
            "shape_id": pd.array([], dtype="string"),
            "shape_pt_lat": pd.array([], dtype="float64"),
            "shape_pt_lon": pd.array([], dtype="float64"),
            "shape_pt_sequence": pd.array([], dtype="int64"),
        })
    return feed