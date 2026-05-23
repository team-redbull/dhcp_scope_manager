from unittest.mock import AsyncMock, patch
import pytest
from app.errors import DhcpConflictError, InvalidScopeIdError
from app.errors import PowerShellError
from app.services.ps_parsers import (
    build_get_all_scopes_script,
    build_get_scope_state_script,
    build_payload_from_scope_state,
    normalize_list,
    normalize_get_scope_state,
    extract_option,
    extract_option_list,
    parse_failover,
    ps_single_quote,
    assemble_scope_state,
)
from app.utils.ip_utils import ip_to_int, parse_timespan_days, parse_timespan_minutes


def test_parse_timespan_days_full():
    assert parse_timespan_days("8.00:00:00") == 8


def test_parse_timespan_days_one_day():
    assert parse_timespan_days("1.00:00:00") == 1


def test_parse_timespan_days_no_dot():
    # No days component → 0
    assert parse_timespan_days("8:00:00") == 0


def test_parse_timespan_minutes_one_hour():
    assert parse_timespan_minutes("1:00:00") == 60


def test_parse_timespan_minutes_half_hour():
    assert parse_timespan_minutes("0:30:00") == 30


def test_parse_timespan_minutes_ninety():
    assert parse_timespan_minutes("1:30:00") == 90


def test_parse_timespan_minutes_day_format_one_day():
    """PowerShell emits d.HH:MM:SS for maxClientLeadTime >= 24h.
    1.00:00:00 = 1 day = 1440 minutes — previously crashed with ValueError."""
    assert parse_timespan_minutes("1.00:00:00") == 1440


def test_parse_timespan_minutes_day_format_half_day():
    """0 days 12 hours — verifies day + time combination."""
    assert parse_timespan_minutes("0.12:00:00") == 720


def test_parse_timespan_minutes_day_format_one_day_thirty():
    """1 day 0 hours 30 minutes = 1470 minutes."""
    assert parse_timespan_minutes("1.00:30:00") == 1470


def test_parse_timespan_minutes_seconds_are_ignored():
    """Seconds component is correctly discarded — 1:30:45 → 90 minutes, not 91."""
    assert parse_timespan_minutes("1:30:45") == 90


def test_parse_timespan_minutes_invalid_format_raises():
    """Unrecognized format must raise ValueError, not silently return 0."""
    with pytest.raises(ValueError, match="Unrecognized PowerShell TimeSpan format"):
        parse_timespan_minutes("00:01:00:00")


def test_ip_to_int_ordering():
    assert ip_to_int("10.20.30.0") < ip_to_int("10.20.40.0")
    assert ip_to_int("10.20.30.1") < ip_to_int("10.20.30.2")
    assert ip_to_int("10.0.0.0") < ip_to_int("11.0.0.0")


def test_normalize_list_dict():
    d = {"key": "val"}
    assert normalize_list(d) == [d]


def test_normalize_list_none():
    assert normalize_list(None) == []


def test_normalize_list_list():
    lst = [{"a": 1}, {"b": 2}]
    assert normalize_list(lst) == lst


def test_normalize_list_scalar_string():
    assert normalize_list("10.0.0.53") == ["10.0.0.53"]


def test_extract_option_router(mock_ps_options_raw):
    result = extract_option(mock_ps_options_raw, 3)
    assert result == "10.20.30.1"


def test_extract_option_dns_list(mock_ps_options_raw):
    result = extract_option_list(mock_ps_options_raw, 6)
    assert result == ["10.0.0.53", "10.0.0.54"]


def test_extract_option_domain(mock_ps_options_raw):
    result = extract_option(mock_ps_options_raw, 15)
    assert result == "lab.local"


def test_extract_option_missing(mock_ps_options_raw):
    assert extract_option(mock_ps_options_raw, 999) == ""
    assert extract_option_list(mock_ps_options_raw, 999) == []


def test_extract_option_scalar_string_value_not_split():
    options = [{"OptionId": 3, "Value": "10.20.30.1"}]
    assert extract_option(options, 3) == "10.20.30.1"


def test_extract_option_list_scalar_string_value_not_split():
    options = [{"OptionId": 6, "Value": "10.0.0.53"}]
    assert extract_option_list(options, 6) == ["10.0.0.53"]


def test_extract_option_list_none_value_returns_empty():
    options = [{"OptionId": 6, "Value": None}]
    assert extract_option_list(options, 6) == []


def _scope_state(scope, options, exclusions=None, failover=None):
    return {
        "scope": scope,
        "options": options,
        "exclusions": [] if exclusions is None else exclusions,
        "failover": failover,
    }


@pytest.mark.asyncio
async def test_assemble_scope_state(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw, mock_ps_failover_raw
):
    with patch(
        "app.services.ps_parsers.run_ps",
        new=AsyncMock(
            return_value=_scope_state(
                mock_ps_scope_raw,
                mock_ps_options_raw,
                mock_ps_exclusions_raw,
                mock_ps_failover_raw,
            )
        ),
    ) as mock_ps:
        result = await assemble_scope_state("10.20.30.0")

    mock_ps.assert_awaited_once()
    _, kwargs = mock_ps.await_args
    assert kwargs["append_error_action"] is False
    assert kwargs["append_convert_to_json"] is False
    assert result.scopeName == "Cluster-A Management"
    assert str(result.network) == "10.20.30.0"
    assert str(result.subnetMask) == "255.255.255.0"
    assert result.leaseDurationDays == 8
    assert str(result.gateway) == "10.20.30.1"
    assert [str(ip) for ip in result.dnsServers] == ["10.0.0.53", "10.0.0.54"]
    assert result.dnsDomain == "lab.local"
    assert len(result.exclusions) == 1
    assert str(result.exclusions[0].startAddress) == "10.20.30.1"
    assert result.failover is not None
    assert result.failover.relationshipName == "mce1-failover"
    assert result.failover.maxClientLeadTimeMinutes == 60


@pytest.mark.asyncio
async def test_assemble_scope_no_failover(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    with patch(
        "app.services.ps_parsers.run_ps",
        new=AsyncMock(
            return_value=_scope_state(
                mock_ps_scope_raw,
                mock_ps_options_raw,
                mock_ps_exclusions_raw,
                None,
            )
        ),
    ):
        result = await assemble_scope_state("10.20.30.0")

    assert result.failover is None


@pytest.mark.asyncio
async def test_assemble_scope_empty_exclusions(mock_ps_scope_raw, mock_ps_options_raw):
    with patch(
        "app.services.ps_parsers.run_ps",
        new=AsyncMock(return_value=_scope_state(mock_ps_scope_raw, mock_ps_options_raw)),
    ):
        result = await assemble_scope_state("10.20.30.0")

    assert result.exclusions == []


@pytest.mark.asyncio
async def test_exclusions_sorted_by_ip(mock_ps_scope_raw, mock_ps_options_raw):
    """Exclusions must come out sorted by IP numeric order regardless of PS output order."""
    unsorted_exclusions = [
        {"StartRange": "10.20.30.201", "EndRange": "10.20.30.254"},
        {"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"},
    ]

    with patch(
        "app.services.ps_parsers.run_ps",
        new=AsyncMock(
            return_value=_scope_state(
                mock_ps_scope_raw,
                mock_ps_options_raw,
                unsorted_exclusions,
                None,
            )
        ),
    ):
        result = await assemble_scope_state("10.20.30.0")

    assert str(result.exclusions[0].startAddress) == "10.20.30.1"
    assert str(result.exclusions[1].startAddress) == "10.20.30.201"


@pytest.mark.asyncio
async def test_assemble_scope_no_exclusions_and_no_failover(mock_ps_scope_raw, mock_ps_options_raw):
    with patch(
        "app.services.ps_parsers.run_ps",
        new=AsyncMock(return_value=_scope_state(mock_ps_scope_raw, mock_ps_options_raw)),
    ):
        result = await assemble_scope_state("10.20.30.0")

    assert result.exclusions == []
    assert result.failover is None


@pytest.mark.asyncio
async def test_unexpected_exclusion_error_still_raises():
    err = PowerShellError("Get-DhcpServerv4ExclusionRange", "Access denied", 5)
    with patch("app.services.ps_parsers.run_ps", new=AsyncMock(side_effect=err)):
        with pytest.raises(PowerShellError):
            await assemble_scope_state("10.20.30.0")


@pytest.mark.asyncio
async def test_unexpected_failover_error_still_raises():
    err = PowerShellError("Get-DhcpServerv4Failover", "RPC server unavailable", 1722)
    with patch("app.services.ps_parsers.run_ps", new=AsyncMock(side_effect=err)):
        with pytest.raises(PowerShellError):
            await assemble_scope_state("10.20.30.0")


def test_single_option_object_is_normalized_to_list(mock_ps_scope_raw, mock_ps_exclusions_raw):
    state = normalize_get_scope_state(
        _scope_state(
            mock_ps_scope_raw,
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            mock_ps_exclusions_raw,
        )
    )
    assert state["options"] == [{"OptionId": 3, "Value": ["10.20.30.1"]}]


def test_single_exclusion_object_is_normalized_to_list(mock_ps_scope_raw, mock_ps_options_raw):
    state = normalize_get_scope_state(
        _scope_state(
            mock_ps_scope_raw,
            mock_ps_options_raw,
            {"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"},
        )
    )
    assert state["exclusions"] == [{"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"}]


def test_returned_payload_comparable_with_post_put_body(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    state = normalize_get_scope_state(
        _scope_state(mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw)
    )
    result = build_payload_from_scope_state("10.20.30.0", state)

    assert result.model_dump(mode="json") == {
        "scopeName": "Cluster-A Management",
        "network": "10.20.30.0",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.30.100",
        "endRange": "10.20.30.200",
        "leaseDurationDays": 8,
        "description": "Cluster A management network",
        "gateway": "10.20.30.1",
        "dnsServers": ["10.0.0.53", "10.0.0.54"],
        "dnsDomain": "lab.local",
        "exclusions": [{"startAddress": "10.20.30.1", "endAddress": "10.20.30.99"}],
        "failover": None,
    }


def test_build_payload_without_gateway_returns_null(mock_ps_scope_raw, mock_ps_exclusions_raw):
    state = normalize_get_scope_state(
        _scope_state(
            mock_ps_scope_raw,
            [{"OptionId": 6, "Value": ["10.0.0.53"]}],
            mock_ps_exclusions_raw,
        )
    )
    result = build_payload_from_scope_state("10.20.30.0", state)

    assert result.gateway is None
    assert result.model_dump(mode="json")["gateway"] is None


def test_build_payload_without_dns_servers_is_invalid_managed_state(mock_ps_scope_raw):
    state = normalize_get_scope_state(
        _scope_state(
            mock_ps_scope_raw,
            [{"OptionId": 3, "Value": ["10.20.30.1"]}],
        )
    )
    with pytest.raises(DhcpConflictError):
        build_payload_from_scope_state("10.20.30.0", state)


def test_invalid_or_unsafe_scope_id_cannot_inject_powershell():
    with pytest.raises(InvalidScopeIdError):
        build_get_scope_state_script("10.20.30.0'; Remove-DhcpServerv4Scope -Force; '")


def test_ps_single_quote_escapes_embedded_single_quote():
    assert ps_single_quote("a'b") == "'a''b'"


def test_get_scope_state_script_contains_depth_10_and_single_scope_literal():
    script = build_get_scope_state_script("10.20.30.0")
    assert "$ScopeId = '10.20.30.0'" in script
    assert "ConvertTo-Json -Depth 10 -Compress" in script
    assert "Get-DhcpServerv4Scope -ScopeId $ScopeId -ErrorAction Stop" in script


def test_get_scope_state_script_rethrows_unexpected_optional_errors():
    script = build_get_scope_state_script("10.20.30.0")
    assert "Test-DhcpNoExclusions" in script
    assert "Test-DhcpNoFailover" in script
    assert script.count("throw") == 2


# ─── build_get_all_scopes_script ──────────────────────────────────────────────

def test_all_scopes_script_contains_required_cmdlets():
    script = build_get_all_scopes_script()
    assert "Get-DhcpServerv4Scope" in script
    assert "Get-DhcpServerv4OptionValue" in script
    assert "Get-DhcpServerv4ExclusionRange" in script
    assert "Get-DhcpServerv4Failover" in script
    assert "ConvertTo-Json -Depth 10 -Compress" in script


def test_all_scopes_script_has_no_scope_id_literal():
    """The all-scopes script must not hard-code a scope ID — it iterates all scopes."""
    script = build_get_all_scopes_script()
    assert "10.20.30.0" not in script
    assert "$ScopeId =" not in script


def test_all_scopes_script_handles_missing_optional_objects():
    """The all-scopes script must use Test-DhcpNoExclusions/Failover helpers for optional data."""
    script = build_get_all_scopes_script()
    assert "Test-DhcpNoExclusions" in script
    assert "Test-DhcpNoFailover" in script
    assert script.count("throw") == 2


def test_all_scopes_script_uses_list_accumulator():
    """The all-scopes script must use a List accumulator and foreach loop."""
    script = build_get_all_scopes_script()
    assert "foreach" in script
    assert "$result" in script
