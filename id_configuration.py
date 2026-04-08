# Configuration: primary table name as key, with id column stored in id_col
ID_CONFIG = {
    "stops": {
        "id_col": "stop_id",
        "identity_cols": ["stop_lat", "stop_lon", "stop_name", "stop_id"], #stops only table with is primary key included in identity_cols. This is because stops are often moved somewhat, but I've been promised they don't change stop_id unless they move it more than 40 meters.
        "foreign_keys": [
            ("stop_times", "stop_id"),
            ("transfers", "from_stop_id"),
            ("transfers", "to_stop_id"),
        ],
    },

    "shapes": {
        "id_col": "shape_id",
        "identity_cols": ["shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
        "foreign_keys": [
            ("trips", "shape_id"),
        ],
    },

    "agency": {
        "id_col": "agency_id",
        "identity_cols": ["agency_name", "agency_timezone"],
        "foreign_keys": [
            ("routes", "agency_id"),
        ],
    },

    "routes": {
        "id_col": "route_id",
        "identity_cols": ["agency_id", "route_short_name", "route_type"],
        "foreign_keys": [
            ("trips", "route_id"),
            ("transfers", "from_route_id"),
            ("transfers", "to_route_id"),
        ],
    },

    "calendar": {
        "id_col": "service_id",
        "identity_cols": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
        "foreign_keys": [
            ("trips", "service_id"),
            ("calendar_dates", "service_id"),
        ],
    },

    "trips": {
        "id_col": "trip_id",
        "identity_cols": ["route_id", "shape_id", "service_id", "trip_headsign", "direction_id"], #should "service_id" be dropped?
        "foreign_keys": [
            ("stop_times", "trip_id"),
            ("transfers", "from_trip_id"),
            ("transfers", "to_trip_id"),
        ],
    },
}

ID_CONFIG_service_id = {
    "calendar": {
        "id_col": "service_id",
        "identity_cols": [
            "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday", "start_date", "end_date"
        ],
        "foreign_keys": [
            ("trips", "service_id"),
            ("calendar_dates", "service_id"),
        ],
    }
}