# IANA timezone names other agencies commonly use for Danish/Central European
# time (UTC+1 winter / UTC+2 summer, EU-wide DST rules) instead of Europe/Copenhagen.
CET_TIMEZONE_ALIASES = {
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Europe/Brussels",
    "Europe/Luxembourg",
    "Europe/Madrid",
    "Europe/Paris",
    "Europe/Rome",
    "Europe/Vienna",
    "Europe/Stockholm",
    "Europe/Oslo",
    "Europe/Warsaw",
    "Europe/Prague",
    "Europe/Budapest",
    "Europe/Zurich",
    "Europe/Ljubljana",
    "Europe/Bratislava",
    "Europe/Belgrade",
    "CET",
}

def normalize_timezones(feed):
    """Collapse the various IANA names agencies use for Central European Time
    down to Europe/Copenhagen, so agency.txt rows that only differ by
    timezone spelling are recognized as the same agency during deduplication."""
    if feed.agency is not None and "agency_timezone" in feed.agency.columns:
        feed.agency["agency_timezone"] = feed.agency["agency_timezone"].replace(
            {tz: "Europe/Copenhagen" for tz in CET_TIMEZONE_ALIASES}
        )
    if feed.stops is not None and "stop_timezone" in feed.stops.columns:
        feed.stops["stop_timezone"] = feed.stops["stop_timezone"].replace(
            {tz: "Europe/Copenhagen" for tz in CET_TIMEZONE_ALIASES}
        )
    return feed
