from __future__ import annotations
from ipaddress import AddressValueError, IPv4Address
import logging
from typing import Any

from app.errors import DhcpConflictError, InvalidScopeError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_executor import run_ps
from app.utils.decorators import log_call
from app.utils.ip_utils import ip_to_int, parse_timespan_days, parse_timespan_minutes
from app.utils.powershell import ps_single_quote

logger = logging.getLogger(__name__)

# PowerShell helper functions shared by both per-scope and all-scopes scripts.
# Inline rather than defined once server-side because each run_ps call is a
# fresh pwsh process with no shared state.
_PS_ABSENCE_HELPERS = r"""
function Test-DhcpNoOptions($ErrorRecord) {
    $message = [string]$ErrorRecord.Exception.Message
    return (
        $message -match '(?i)(option|value)' -and
        $message -match '(?i)(not found|cannot find|does not exist|not set|none|no .*option)'
    )
}

function Test-DhcpNoExclusions($ErrorRecord) {
    $message = [string]$ErrorRecord.Exception.Message
    return (
        $message -match '(?i)exclusion' -and
        $message -match '(?i)(not found|cannot find|does not exist|no .*exclusion)'
    )
}
""".strip()


def _extract_ip_str(val: object) -> str:
    """Extract a plain IPv4 dotted-decimal string from a PowerShell-serialized value.

    ConvertTo-Json serializes .NET IPAddress objects as dicts; the reliable
    human-readable key is 'IPAddressToString'. Falls back to plain str for
    values that are already strings.
    """
    if isinstance(val, dict):
        ip_str = val.get("IPAddressToString")
        if ip_str:
            return str(ip_str)
        raise ValueError(f"IPAddress dict missing 'IPAddressToString': {val!r}")
    return str(val)


def normalize_list(result) -> list:
    """Normalize PowerShell JSON None/scalar/object/list output into a list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, tuple):
        return list(result)
    return [result]


def _parse_timespan_as_minutes(val: object, default: int = 60) -> int:
    """Parse a PowerShell TimeSpan value to total minutes.

    Handles three serialization formats that Windows DHCP can produce:
      str   — "1:00:00" or "1.00:00:00"  (CIM string properties)
      dict  — {"TotalMinutes": 60.0, ...} (ConvertTo-Json of a .NET TimeSpan)
      int/float — interpret directly as minutes (edge-case fallback)

    A None input returns `default` (1 hour) so callers do not need to guard.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        total = val.get("TotalMinutes")
        if total is not None:
            return int(total)
        days = int(val.get("Days") or 0)
        hours = int(val.get("Hours") or 0)
        minutes = int(val.get("Minutes") or 0)
        return days * 24 * 60 + hours * 60 + minutes
    return parse_timespan_minutes(str(val))


def _parse_timespan_as_days(val: object, default: int = 8) -> int:
    """Parse a PowerShell TimeSpan value to total days.

    Same format handling as _parse_timespan_as_minutes.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        return int(val.get("Days") or default)
    return parse_timespan_days(str(val))


def _validated_scope(scope: str) -> str:
    scope_text = str(scope)
    try:
        return str(IPv4Address(scope_text))
    except (AddressValueError, ValueError):
        raise InvalidScopeError(scope_text)


def build_get_all_scopes_script() -> str:
    """Build a single PowerShell script that emits ALL scope states as a JSON array.

    Runs one PowerShell process for the entire fleet instead of one per scope.
    The output structure is identical to build_get_scope_state_script — an array
    where each element is {scope, options, exclusions, failover}.

    Failover is resolved by fetching all relationships once before the loop and
    matching by ScopeId inside Python/PowerShell — this avoids treating a scope
    that is not in any failover relationship as a PowerShell error.

    PowerShell collapses a single-element array to a plain object in ConvertTo-Json.
    normalize_list() on the parsed result handles both cases transparently.
    """
    return _PS_ABSENCE_HELPERS + r"""

$allScopes = @(Get-DhcpServerv4Scope -ErrorAction Stop)
$allFailovers = @(Get-DhcpServerv4Failover -ErrorAction Stop)
$result = New-Object System.Collections.Generic.List[Object]

foreach ($s in $allScopes) {
    $sid = $s.ScopeId.ToString()

    try {
        $options = @(Get-DhcpServerv4OptionValue -ScopeId $s.ScopeId -ErrorAction Stop)
    } catch {
        if (Test-DhcpNoOptions $_) {
            $options = @()
        } else {
            throw
        }
    }

    try {
        $exclusions = @(Get-DhcpServerv4ExclusionRange -ScopeId $s.ScopeId -ErrorAction Stop)
    } catch {
        if (Test-DhcpNoExclusions $_) {
            $exclusions = @()
        } else {
            throw
        }
    }

    $failover = $null
    foreach ($fo in $allFailovers) {
        $foScopes = @($fo.ScopeId) | ForEach-Object { $_.ToString() }
        if ($foScopes -contains $sid) {
            $failover = $fo
            break
        }
    }

    $result.Add([PSCustomObject]@{
        scope     = $s
        options   = $options
        exclusions = $exclusions
        failover  = $failover
    })
}

$result | ConvertTo-Json -Depth 10 -Compress
""".strip()


def build_get_scope_state_script(scope: str) -> str:
    """Build a single PowerShell script that emits all scope state as JSON.

    Failover is resolved via an aggregate Get-DhcpServerv4Failover call (no -ScopeId)
    so that a scope not participating in any failover relationship is never treated as
    a PowerShell error — the filter simply yields $null, which normalizes to failover: null.
    """
    scope_literal = ps_single_quote(_validated_scope(scope))
    return (
        f"$ScopeId = {scope_literal}\n\n"
        + _PS_ABSENCE_HELPERS
        + f"""

$scope = Get-DhcpServerv4Scope -ScopeId $ScopeId -ErrorAction Stop

try {{
    $options = @(Get-DhcpServerv4OptionValue -ScopeId $ScopeId -ErrorAction Stop)
}} catch {{
    if (Test-DhcpNoOptions $_) {{
        $options = @()
    }} else {{
        throw
    }}
}}

try {{
    $exclusions = @(Get-DhcpServerv4ExclusionRange -ScopeId $ScopeId -ErrorAction Stop)
}} catch {{
    if (Test-DhcpNoExclusions $_) {{
        $exclusions = @()
    }} else {{
        throw
    }}
}}

$allFailovers = @(Get-DhcpServerv4Failover -ErrorAction Stop)
$failover = $null
foreach ($fo in $allFailovers) {{
    $foScopes = @($fo.ScopeId) | ForEach-Object {{ $_.ToString() }}
    if ($foScopes -contains $ScopeId) {{
        $failover = $fo
        break
    }}
}}

$result = [PSCustomObject]@{{
    scope = $scope
    options = $options
    exclusions = $exclusions
    failover = $failover
}}

$result | ConvertTo-Json -Depth 10 -Compress
"""
    ).strip()


def extract_option(options: list, option_id: int) -> str:
    """Extract the first value of a DHCP option by OptionId."""
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("OptionId") == option_id:
            values = normalize_list(opt.get("Value"))
            if values:
                return _extract_ip_str(values[0])
    return ""


def extract_optional_option(options: list, option_id: int) -> str | None:
    """Extract the first value of an optional DHCP option, or None when absent."""
    value = extract_option(options, option_id)
    return value or None


def extract_option_list(options: list, option_id: int) -> list[str]:
    """Extract all values of a DHCP option by OptionId as a list."""
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("OptionId") == option_id:
            return [_extract_ip_str(v) for v in normalize_list(opt.get("Value"))]
    return []


def parse_failover(raw: dict) -> DhcpFailover:
    """Parse a PowerShell Get-DhcpServerv4Failover result into DhcpFailover.

    Defensive against three edge cases:
    - None for ReservePercent / LoadBalancePercent (int(None) would TypeError)
    - serverRole absent from PS output (must not silently default to 'Active' for HotStandby
      because the actual standby server would report wrong parity; let model validator enforce)
    - MaxClientLeadTime as a JSON TimeSpan object (ConvertTo-Json of a .NET TimeSpan)
      rather than the usual CIM string "1:00:00"
    """
    return DhcpFailover(
        partnerServer=str(raw.get("PartnerServer") or ""),
        relationshipName=str(raw.get("Name") or ""),
        mode=raw.get("Mode", "HotStandby"),
        serverRole=raw.get("ServerRole") or None,
        reservePercent=int(raw.get("ReservePercent") or 0),
        loadBalancePercent=int(raw.get("LoadBalancePercent") or 0),
        maxClientLeadTimeMinutes=_parse_timespan_as_minutes(
            raw.get("MaxClientLeadTime"), default=60
        ),
    )


def normalize_get_scope_state(raw: Any) -> dict[str, Any]:
    """Normalize the combined JSON object emitted by build_get_scope_state_script."""
    if not isinstance(raw, dict):
        raise ValueError("Expected DHCP scope state JSON object")

    return {
        "scope": raw.get("scope") or {},
        "options": normalize_list(raw.get("options")),
        "exclusions": normalize_list(raw.get("exclusions")),
        "failover": raw.get("failover"),
    }


def build_payload_from_scope_state(scope: str, state: dict[str, Any]) -> DhcpScopePayload:
    scope = _validated_scope(scope)
    scope_raw = state["scope"]
    options = state["options"]
    exclusions_list = state["exclusions"]

    failover_raw = state["failover"]
    failover_obj: DhcpFailover | None = None
    if failover_raw:
        failover_entries = normalize_list(failover_raw)
        if failover_entries:
            failover_obj = parse_failover(failover_entries[0])

    # Parse lease duration: handles string "8.00:00:00", JSON TimeSpan dict, or int
    lease_days = _parse_timespan_as_days(scope_raw.get("LeaseDuration"), default=8)
    dns_servers = extract_option_list(options, 6)
    if not dns_servers:
        raise DhcpConflictError(
            f"Observed DHCP scope {scope} is missing required DNS servers"
        )

    # Build sorted exclusions; skip any non-dict entries (e.g. null from PS)
    exclusions = sorted(
        [
            DhcpExclusion(
                startAddress=IPv4Address(_extract_ip_str(e["StartRange"])),
                endAddress=IPv4Address(_extract_ip_str(e["EndRange"])),
            )
            for e in exclusions_list
            if isinstance(e, dict)
        ],
        key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
    )

    raw_gateway = extract_optional_option(options, 3)
    return DhcpScopePayload(
        scope=IPv4Address(scope),
        scopeName=str(scope_raw.get("Name") or ""),
        subnetMask=IPv4Address(_extract_ip_str(scope_raw.get("SubnetMask", ""))),
        startRange=IPv4Address(_extract_ip_str(scope_raw.get("StartRange", ""))),
        endRange=IPv4Address(_extract_ip_str(scope_raw.get("EndRange", ""))),
        leaseDurationDays=lease_days,
        description=str(scope_raw.get("Description") or ""),
        gateway=IPv4Address(raw_gateway) if raw_gateway else None,
        dnsServers=[IPv4Address(s) for s in dns_servers],
        dnsDomain=extract_option(options, 15),
        # Options 66/67 are optional: extract_option returns "" when the server has
        # no such option value, which is exactly the model's "no PXE" default, so an
        # absent pair round-trips to the same "" the desired body carries.
        nextServer=extract_option(options, 66),
        bootFile=extract_option(options, 67),
        exclusions=exclusions,
        failover=failover_obj,
    )


@log_call
async def assemble_scope_state(scope: str) -> DhcpScopePayload:
    """Query Windows DHCP once and assemble the canonical DhcpScopePayload.

    This is the single source of truth for GET responses. The output MUST be
    byte-for-byte comparable to the PUT/POST body Crossplane sends.
    """
    script = build_get_scope_state_script(scope)
    raw = await run_ps(
        script,
        append_error_action=False,
        append_convert_to_json=False,
        scope=scope,
        operation="get_scope_state",
    )
    state = normalize_get_scope_state(raw)
    return build_payload_from_scope_state(scope, state)
