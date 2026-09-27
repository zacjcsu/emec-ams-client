from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now_str():
    """Current time as the naive-UTC string the server stores (timestamp without time zone)."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)
