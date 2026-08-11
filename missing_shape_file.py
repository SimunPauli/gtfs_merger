import pandas as pd

def normalize_missing_shapes(feed):
    """Make a feed without shapes.txt indistinguishable from a feed
    with an empty (header-only) shapes.txt. And make shape_id in trips
    NaN if column exist."""
    if feed.trips is not None and "shape_id" not in feed.trips.columns:
        feed.trips = feed.trips.assign(
            shape_id=pd.array([pd.NA] * len(feed.trips), dtype="string")
        )
        #Remove shape_id from trips
    if feed.shapes is None and "shape_id" in feed.trips.columns:
        feed.trips['shape_id'] = pd.NA
    if feed.shapes is None:
        feed.shapes = pd.DataFrame({
            "shape_id": pd.array([], dtype="string"),
            "shape_pt_lat": pd.array([], dtype="float64"),
            "shape_pt_lon": pd.array([], dtype="float64"),
            "shape_pt_sequence": pd.array([], dtype="int64"),
        })
    return feed