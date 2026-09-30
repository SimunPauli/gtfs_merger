import numpy as np

def rejseplan_stop_id_base(stop_ids):
	"""Strip the trailing "G"/"_G" variants Rejseplan appends to re-emitted stop_ids
	("...8600669G", "...8600669GG", "...8600053_G_G" -> "...8600669", "...8600053")."""
	return stop_ids.str.replace(r"(_?G)+$", "", regex=True)


def distance_m(lat, lon, lat_other, lon_other):
	"""Approximate distance in metres (equirectangular; fine at stop-to-stop scale)."""
	return np.hypot(
		(lat - lat_other) * 111_320,
		(lon - lon_other) * 111_320 * np.cos(np.radians(lat)),
	)


def validate_stop_coordinates(feed, file_name):
	"""
	GTFS requires stop_lat/stop_lon to be WGS84 decimal degrees. Some DTU releases
	have shipped stops in a projected CRS (e.g. UTM32N/ETRS89) instead, which silently
	produces valid-looking numbers that only fail hours later inside OTP.
	"""
	bad = feed.stops.loc[
		~feed.stops["stop_lat"].between(-90, 90) | ~feed.stops["stop_lon"].between(-180, 180)
	]
	if not bad.empty:
		example = bad.iloc[0]
		raise ValueError(
			f"{file_name}: {len(bad)} stop(s) have stop_lat/stop_lon outside the valid "
			f"WGS84 range (likely a projected CRS, not WGS84 degrees) -- e.g. "
			f"stop_id={example['stop_id']!r} lat={example['stop_lat']} lon={example['stop_lon']}"
		)


def check_split_stops(stops, max_distance_m=5):
	"""
	Find physical stops left under two stop_ids after deduplication: same stop_name, within
	max_distance_m, from different feeds. OTP sees two stops, so the year's service is split
	between them and a via constraint on one misses the other's trips.
	Raises if both share a Rejseplan stop_id (a deduplication bug, e.g. "...8600053" vs
	"...8600053_G_G"); only warns if the Rejseplan stop_ids differ, as stop dedup keys on stop_id.
	"""
	named = stops.loc[
		stops.duplicated("stop_name", keep=False),
		["stop_id", "stop_name", "stop_lat", "stop_lon", "feed_id", "feed_id_last"],
	]
	pairs = named.merge(named, on="stop_name", suffixes=("", "_other"))
	pairs = pairs.loc[(pairs["stop_id"] < pairs["stop_id_other"]) & (pairs["feed_id"] != pairs["feed_id_other"])]
	dist_m = distance_m(pairs["stop_lat"], pairs["stop_lon"], pairs["stop_lat_other"], pairs["stop_lon_other"])
	pairs = pairs.loc[dist_m < max_distance_m]
	if pairs.empty:
		return

	def rejseplan_id(ids):  # drop the feed prefix, then the "G" variants
		return rejseplan_stop_id_base(ids.str.split("_", n=1).str[1])

	def examples(df):
		return "\n".join(f"  {r.stop_name}: {r.stop_id} / {r.stop_id_other}" for r in df.head(10).itertuples())

	same_id = rejseplan_id(pairs["stop_id"]) == rejseplan_id(pairs["stop_id_other"])
	other_id = pairs.loc[~same_id]
	if not other_id.empty:
		# both ids in the same feeds = published side by side; otherwise one replaced the other
		coexist = (other_id["feed_id"] <= other_id["feed_id_last_other"]) & (other_id["feed_id_other"] <= other_id["feed_id_last"])
		print(f"Warning: {len(other_id)} stop pair(s) with the same name <{max_distance_m} m apart survive as two stops:")
		for label, df in [("published side by side", other_id.loc[coexist]), ("renumbered by Rejseplan", other_id.loc[~coexist])]:
			if not df.empty:
				print(f" {len(df)} {label}, e.g.:\n{examples(df)}")
	split = pairs.loc[same_id]
	if not split.empty:
		raise ValueError(
			f"{len(split)} stop(s) survived deduplication under two stop_ids despite the same "
			f"Rejseplan stop_id, name and location (<{max_distance_m} m), e.g.:\n{examples(split)}"
		)

