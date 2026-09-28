from __future__ import annotations
from ipaddress import IPv4Address


def ip_to_int(ip: str | IPv4Address) -> int:
    """Convert an IPv4 address (string or IPv4Address) to an integer for numeric sorting."""
    if isinstance(ip, IPv4Address):
        return int(ip)
    return int(IPv4Address(ip))


def parse_timespan_days(ts: str) -> int:
    """Parse a PowerShell TimeSpan string to days.

    Handles:
      "8.00:00:00"  -> 8   (days.HH:MM:SS)
      "8:00:00"     -> 0   (no days component — treat as 0 days)
    """
    if "." in ts:
        return int(ts.split(".")[0])
    return 0


def parse_timespan_minutes(ts: str) -> int:
    """Parse a PowerShell TimeSpan string to total minutes.

    Handles:
      "1:00:00"    -> 60    (HH:MM:SS)
      "0:30:00"    -> 30
      "1:30:00"    -> 90
      "1.00:00:00" -> 1440  (d.HH:MM:SS — PowerShell emits this for values >= 24h)
      "0.12:00:00" -> 720   (0 days 12 hours)
    """
    days = 0
    time_part = ts
    if "." in ts:
        day_str, time_part = ts.split(".", 1)
        try:
            days = int(day_str)
        except ValueError:
            days = 0

    parts = time_part.split(":")
    if len(parts) == 3:
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            return days * 24 * 60 + hours * 60 + minutes
        except ValueError:
            pass
    raise ValueError(
        f"Unrecognized PowerShell TimeSpan format: {ts!r}. "
        f"Expected 'HH:MM:SS' or 'd.HH:MM:SS'."
    )
