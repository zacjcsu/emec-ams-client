import time
from datetime import datetime, timedelta, timezone

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# After a power cut the Pi's clock is wrong until NTP syncs. Once the server has answered, time is counted from
# the server's clock on the monotonic clock, which an NTP jump doesn't move.
_server_anchor = None


def set_server_time(server_utc):
    """`server_utc` is the server's naive UTC now()."""
    global _server_anchor
    _server_anchor = (server_utc.replace(tzinfo=timezone.utc), time.monotonic())


def utc_now():
    if _server_anchor is None:
        return datetime.now(timezone.utc)
    at, mono = _server_anchor
    return at + timedelta(seconds=time.monotonic() - mono)


def utc_now_str():
    """Current time as the naive-UTC string the server stores (timestamp without time zone)."""
    return utc_now().strftime(TS_FORMAT)
