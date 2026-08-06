from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address
from typing import Iterable

from app.errors import InvalidScopeError


def ps_single_quote(value: object) -> str:
    """Return a PowerShell single-quoted string literal."""
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def ps_ipv4(value: object) -> str:
    """Validate an IPv4 value and return it as a PowerShell string literal."""
    text = str(value)
    try:
        return ps_single_quote(str(IPv4Address(text)))
    except (AddressValueError, ValueError):
        raise InvalidScopeError(text)


def ps_ipv4_csv(values: Iterable[object]) -> str:
    """Return a comma-separated PowerShell IPv4 literal list."""
    return ",".join(ps_ipv4(value) for value in values)
