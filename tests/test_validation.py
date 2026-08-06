"""Tests for Pydantic model validation — IP addresses, ranges, field constraints."""
import pytest
from pydantic import ValidationError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload


# ---------------------------------------------------------------------------
# DhcpExclusion
# ---------------------------------------------------------------------------

def test_exclusion_valid():
    e = DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")
    assert str(e.startAddress) == "10.20.30.1"
    assert str(e.endAddress) == "10.20.30.99"


def test_exclusion_same_start_end_valid():
    # Single IP exclusion is valid
    e = DhcpExclusion(startAddress="10.20.30.5", endAddress="10.20.30.5")
    assert str(e.startAddress) == "10.20.30.5"


def test_exclusion_end_before_start_invalid():
    with pytest.raises(ValidationError) as exc_info:
        DhcpExclusion(startAddress="10.20.30.99", endAddress="10.20.30.1")
    assert "endAddress" in str(exc_info.value)


def test_exclusion_invalid_ip_octet():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="10.20.999.1", endAddress="10.20.30.99")


def test_exclusion_invalid_ip_format():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="not-an-ip", endAddress="10.20.30.99")


def test_exclusion_invalid_ip_too_few_octets():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="10.20.30", endAddress="10.20.30.99")


# ---------------------------------------------------------------------------
# DhcpFailover
# ---------------------------------------------------------------------------

def test_failover_valid(sample_failover):
    assert sample_failover.reservePercent == 5
    assert sample_failover.maxClientLeadTimeMinutes == 60


def test_failover_reserve_percent_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=101,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_reserve_percent_negative():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=-1,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_load_balance_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="LoadBalance",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=101,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_max_lead_time_zero():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=0,
        )


def test_failover_max_lead_time_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=1441,
        )


def test_failover_empty_partner_server():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_invalid_mode():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="InvalidMode",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_invalid_server_role():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Primary",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


# ---------------------------------------------------------------------------
# DhcpScopePayload
# ---------------------------------------------------------------------------

def test_scope_payload_valid(sample_scope_payload):
    assert str(sample_scope_payload.scope) == "10.20.30.0"
    assert str(sample_scope_payload.gateway) == "10.20.30.1"


def test_scope_gateway_omitted_derives_dot_254():
    """Omitting the key entirely derives the subnet's .254 address."""
    payload = DhcpScopePayload(
        scopeName="Test",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="",
        dnsServers=["10.0.0.53"],
        dnsDomain="",
        exclusions=[],
    )
    assert str(payload.gateway) == "10.20.30.254"


def test_scope_gateway_explicit_none_stays_none():
    """An explicit null is honoured as written — no DHCP option 3.

    This is the state that must stay distinguishable from omission, otherwise a
    scope deliberately configured without a router option could never be expressed.
    """
    payload = DhcpScopePayload(
        scopeName="Test",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="",
        gateway=None,
        dnsServers=["10.0.0.53"],
        dnsDomain="",
        exclusions=[],
    )
    assert payload.gateway is None


def test_scope_gateway_empty_string_normalizes_to_none():
    payload = DhcpScopePayload(
        scopeName="Test",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="",
        gateway="",
        dnsServers=["10.0.0.53"],
        dnsDomain="",
        exclusions=[],
    )
    assert payload.gateway is None


def test_scope_end_range_before_start_range():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.200",
            endRange="10.20.30.100",  # before startRange
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="lab.local",
            exclusions=[],
        )
    assert "endRange" in str(exc_info.value)


def test_scope_invalid_network_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.999.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_invalid_gateway_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="not-an-ip",
            dnsServers=["10.0.0.53"],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_invalid_dns_server_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53", "300.0.0.1"],  # invalid IP
            dnsDomain="",
            exclusions=[],
        )


def test_scope_lease_duration_zero():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=0,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_lease_duration_too_high():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=3651,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_empty_name_invalid():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="",
            scope="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_json_serialization_uses_strings(sample_scope_payload):
    """IPv4Address fields must serialize as plain strings in JSON output."""
    data = sample_scope_payload.model_dump(mode="json")
    assert isinstance(data["scope"], str)
    assert isinstance(data["gateway"], str)
    assert isinstance(data["subnetMask"], str)
    assert all(isinstance(ip, str) for ip in data["dnsServers"])
    assert isinstance(data["exclusions"][0]["startAddress"], str)


# ---------------------------------------------------------------------------
# Subnet consistency validation
# ---------------------------------------------------------------------------

def _minimal_scope(**overrides):
    """Minimal valid DhcpScopePayload for subnet tests."""
    base = dict(
        scopeName="Test",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53"],
        dnsDomain="",
        exclusions=[],
    )
    base.update(overrides)
    return base


def test_subnet_valid_config_passes():
    """A fully valid config must not raise."""
    DhcpScopePayload(**_minimal_scope())


def test_subnet_network_with_host_bits_invalid():
    """network must be a network address — host bits must be zero."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(scope="10.20.30.5"))
    assert "valid subnet" in str(exc_info.value).lower()


def test_subnet_non_contiguous_mask_invalid():
    """subnetMask must be a contiguous prefix mask."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(subnetMask="255.255.0.255"))
    assert "valid subnet" in str(exc_info.value).lower()


def test_subnet_start_range_outside_subnet():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            startRange="10.20.31.100",
            endRange="10.20.31.200",
        ))
    assert "startRange" in str(exc_info.value)


def test_subnet_end_range_outside_subnet():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(endRange="10.20.31.200"))
    assert "endRange" in str(exc_info.value)


def test_subnet_gateway_outside_subnet():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(gateway="192.168.1.1"))
    assert "gateway" in str(exc_info.value)


def test_subnet_exclusion_start_outside_subnet():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            exclusions=[{"startAddress": "10.20.31.1", "endAddress": "10.20.31.10"}],
        ))
    assert "exclusions[0].startAddress" in str(exc_info.value)


def test_subnet_exclusion_end_outside_subnet():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            exclusions=[{"startAddress": "10.20.30.1", "endAddress": "10.20.31.10"}],
        ))
    assert "exclusions[0].endAddress" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Derived defaults for subnetMask and gateway
# ---------------------------------------------------------------------------

def _without(*keys, **overrides):
    """_minimal_scope with the named keys removed, to model an omitted body key."""
    scope = _minimal_scope(**overrides)
    for key in keys:
        scope.pop(key, None)
    return scope


@pytest.mark.parametrize("mask", [None, ""], ids=["null", "empty"])
def test_subnet_mask_empty_forms_default(mask):
    """subnetMask has no 'unset' meaning, so null and "" collapse to the default."""
    payload = DhcpScopePayload(**_minimal_scope(subnetMask=mask))
    assert str(payload.subnetMask) == "255.255.255.0"


def test_subnet_mask_omitted_defaults():
    payload = DhcpScopePayload(**_without("subnetMask"))
    assert str(payload.subnetMask) == "255.255.255.0"


def test_both_omitted_resolve_together():
    payload = DhcpScopePayload(**_without("subnetMask", "gateway"))
    assert str(payload.subnetMask) == "255.255.255.0"
    assert str(payload.gateway) == "10.20.30.254"


def test_gateway_derived_from_scope_not_hardcoded():
    """The default tracks the scope address rather than a fixed prefix."""
    payload = DhcpScopePayload(
        **_without("gateway", scope="192.168.7.0", startRange="192.168.7.100",
                   endRange="192.168.7.200")
    )
    assert str(payload.gateway) == "192.168.7.254"


def test_non_24_mask_without_gateway_is_mismatch():
    """No .254 convention exists off a /24 — reject rather than guess."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_without(
            "gateway", scope="10.20.0.0", subnetMask="255.255.0.0",
            startRange="10.20.30.100", endRange="10.20.30.200",
        ))
    message = str(exc_info.value)
    assert "gateway is required when subnetMask is 255.255.0.0" in message


def test_non_24_mask_with_explicit_gateway_is_valid():
    """The mismatch guard is scoped to the derive path, not to non-/24 masks generally."""
    payload = DhcpScopePayload(**_minimal_scope(
        scope="10.20.0.0", subnetMask="255.255.0.0", gateway="10.20.0.1",
        startRange="10.20.30.100", endRange="10.20.30.200",
    ))
    assert str(payload.gateway) == "10.20.0.1"


def test_non_24_mask_with_explicit_null_gateway_is_valid():
    """Explicitly asking for no gateway is not a mismatch — nothing needs deriving."""
    payload = DhcpScopePayload(**_minimal_scope(
        scope="10.20.0.0", subnetMask="255.255.0.0", gateway=None,
        startRange="10.20.30.100", endRange="10.20.30.200",
    ))
    assert payload.gateway is None


def test_derived_gateway_still_subject_to_distribution_range_guard():
    """A derived .254 gets the same safety checks as one written by hand.

    Without this, a range ending at .254 would silently hand the router's address
    out on a lease.
    """
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_without("gateway", endRange="10.20.30.254"))
    assert "not covered by any exclusion" in str(exc_info.value)


def test_derived_gateway_accepted_when_excluded():
    payload = DhcpScopePayload(**_without(
        "gateway",
        endRange="10.20.30.254",
        exclusions=[{"startAddress": "10.20.30.250", "endAddress": "10.20.30.254"}],
    ))
    assert str(payload.gateway) == "10.20.30.254"


# ---------------------------------------------------------------------------
# Network/broadcast address role enforcement
# ---------------------------------------------------------------------------

def test_gateway_is_network_address_invalid():
    """gateway must not be the network address."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(gateway="10.20.30.0"))
    assert "gateway" in str(exc_info.value)
    assert "network address" in str(exc_info.value).lower()


def test_gateway_is_broadcast_address_invalid():
    """gateway must not be the broadcast address."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(gateway="10.20.30.255"))
    assert "gateway" in str(exc_info.value)
    assert "broadcast address" in str(exc_info.value).lower()


def test_start_range_is_network_address_invalid():
    """startRange must not be the network address."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(startRange="10.20.30.0", endRange="10.20.30.200"))
    assert "startRange" in str(exc_info.value)


def test_end_range_is_broadcast_address_invalid():
    """endRange must not be the broadcast address."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(endRange="10.20.30.255"))
    assert "endRange" in str(exc_info.value)


# ---------------------------------------------------------------------------
# DhcpFailover mode-specific field enforcement
# ---------------------------------------------------------------------------

def _failover(**overrides):
    """Minimal valid HotStandby DhcpFailover."""
    base = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="rel1",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        maxClientLeadTimeMinutes=60,
    )
    base.update(overrides)
    return DhcpFailover(**base)


def test_hotstandby_requires_server_role():
    """serverRole must be provided for HotStandby mode."""
    with pytest.raises(ValidationError) as exc_info:
        _failover(serverRole=None)
    assert "serverRole" in str(exc_info.value)
    assert "HotStandby" in str(exc_info.value)


def test_hotstandby_normalizes_loadbalancepercent_to_zero():
    """loadBalancePercent is not used in HotStandby — always normalized to 0."""
    f = _failover(loadBalancePercent=75)
    assert f.loadBalancePercent == 0


def test_hotstandby_loadbalancepercent_omitted_normalizes_to_zero():
    """loadBalancePercent omitted for HotStandby — must still serialize as 0."""
    f = _failover()
    assert f.loadBalancePercent == 0


def test_loadbalance_requires_loadbalancepercent():
    """loadBalancePercent must be provided for LoadBalance mode."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="LoadBalance",
            maxClientLeadTimeMinutes=60,
        )
    assert "loadBalancePercent" in str(exc_info.value)
    assert "LoadBalance" in str(exc_info.value)


def test_loadbalance_normalizes_serverrole_to_active():
    """serverRole is not used in LoadBalance — always normalized to 'Active'."""
    f = DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="rel1",
        mode="LoadBalance",
        loadBalancePercent=50,
        serverRole="Standby",  # ignored — normalized to Active
        maxClientLeadTimeMinutes=60,
    )
    assert f.serverRole == "Active"


def test_loadbalance_normalizes_reservepercent_to_zero():
    """reservePercent is not used in LoadBalance — always normalized to 0."""
    f = DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="rel1",
        mode="LoadBalance",
        loadBalancePercent=60,
        reservePercent=10,  # ignored — normalized to 0
        maxClientLeadTimeMinutes=60,
    )
    assert f.reservePercent == 0


def test_loadbalance_valid_full():
    """A complete valid LoadBalance config must be accepted."""
    f = DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="rel1",
        mode="LoadBalance",
        loadBalancePercent=50,
        maxClientLeadTimeMinutes=60,
    )
    assert f.mode == "LoadBalance"
    assert f.loadBalancePercent == 50
    assert f.serverRole == "Active"
    assert f.reservePercent == 0


# ---------------------------------------------------------------------------
# description null normalization
# ---------------------------------------------------------------------------

def test_description_none_normalizes_to_empty_string():
    """description=None must normalize to '' — prevents Crossplane drift."""
    scope = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
        description=None,  # explicit null
        gateway="10.0.0.1", dnsServers=["10.0.0.53"], dnsDomain="", exclusions=[],
    )
    assert scope.description == ""


def test_description_empty_string_accepted():
    """description='' must be accepted and preserved."""
    scope = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
        description="",
        gateway="10.0.0.1", dnsServers=["10.0.0.53"], dnsDomain="", exclusions=[],
    )
    assert scope.description == ""


# ---------------------------------------------------------------------------
# Overlapping exclusion ranges
# ---------------------------------------------------------------------------

def test_overlapping_exclusions_rejected():
    """Exclusion ranges that overlap must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            exclusions=[
                {"startAddress": "10.20.30.1", "endAddress": "10.20.30.20"},
                {"startAddress": "10.20.30.10", "endAddress": "10.20.30.30"},
            ]
        ))
    assert "overlap" in str(exc_info.value).lower()


def test_contained_exclusion_rejected():
    """One exclusion range contained entirely within another is an overlap."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            exclusions=[
                {"startAddress": "10.20.30.1", "endAddress": "10.20.30.50"},
                {"startAddress": "10.20.30.10", "endAddress": "10.20.30.20"},
            ]
        ))
    assert "overlap" in str(exc_info.value).lower()


def test_adjacent_exclusions_not_overlapping():
    """Exclusions that share a boundary (end == start of next) do overlap; end+1 == start does not."""
    # These are adjacent (touching) — should NOT overlap: endAddress .10 and startAddress .11
    DhcpScopePayload(**_minimal_scope(
        exclusions=[
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
            {"startAddress": "10.20.30.11", "endAddress": "10.20.30.20"},
        ]
    ))  # must not raise


def test_touching_exclusions_at_same_ip_rejected():
    """Exclusions where endAddress == startAddress of next do overlap (single shared IP)."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            exclusions=[
                {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
                {"startAddress": "10.20.30.10", "endAddress": "10.20.30.20"},
            ]
        ))
    assert "overlap" in str(exc_info.value).lower()


def test_non_overlapping_exclusions_accepted():
    """Two well-separated exclusion ranges must be accepted."""
    DhcpScopePayload(**_minimal_scope(
        exclusions=[
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
            {"startAddress": "10.20.30.20", "endAddress": "10.20.30.30"},
        ]
    ))  # must not raise


# ---------------------------------------------------------------------------
# partnerServer / relationshipName whitespace-only rejection
# ---------------------------------------------------------------------------

def test_partner_server_whitespace_only_rejected():
    """partnerServer of only spaces must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpFailover(
            partnerServer="   ",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            maxClientLeadTimeMinutes=60,
        )
    assert "whitespace" in str(exc_info.value).lower() or "blank" in str(exc_info.value).lower()


def test_relationship_name_whitespace_only_rejected():
    """relationshipName of only spaces must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="   ",
            mode="HotStandby",
            serverRole="Active",
            maxClientLeadTimeMinutes=60,
        )
    assert "whitespace" in str(exc_info.value).lower() or "blank" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# dnsDomain null normalization
# ---------------------------------------------------------------------------

def test_dns_domain_none_normalizes_to_empty_string():
    """dnsDomain=None must normalize to '' — consistent with description field."""
    scope = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
        description="", gateway="10.0.0.1", dnsServers=["10.0.0.53"], dnsDomain=None, exclusions=[],
    )
    assert scope.dnsDomain == ""


# ---------------------------------------------------------------------------
# Gateway-in-distribution-range validation
# ---------------------------------------------------------------------------

def test_gateway_outside_range_accepted():
    """Gateway below startRange is outside the pool — valid."""
    scope = DhcpScopePayload(**_minimal_scope(
        gateway="10.20.30.1",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        exclusions=[],
    ))
    assert str(scope.gateway) == "10.20.30.1"


def test_gateway_none_accepted():
    """gateway=None means no router option — always valid."""
    scope = DhcpScopePayload(**_minimal_scope(gateway=None))
    assert scope.gateway is None


def test_gateway_in_range_without_exclusion_rejected():
    """Gateway inside [startRange, endRange] with no exclusion causes DHCP network outage."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            gateway="10.20.30.150",   # inside 100–200
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            exclusions=[],
        ))
    assert "distribution range" in str(exc_info.value).lower()


def test_gateway_in_range_with_exclusion_accepted():
    """Gateway inside range is valid when it is covered by an exclusion."""
    scope = DhcpScopePayload(**_minimal_scope(
        gateway="10.20.30.101",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        exclusions=[
            {"startAddress": "10.20.30.100", "endAddress": "10.20.30.110"},
        ],
    ))
    assert str(scope.gateway) == "10.20.30.101"


def test_gateway_at_start_of_range_without_exclusion_rejected():
    """Gateway equal to startRange with no exclusion is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            gateway="10.20.30.100",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            exclusions=[],
        ))
    assert "distribution range" in str(exc_info.value).lower()


def test_gateway_at_end_of_range_without_exclusion_rejected():
    """Gateway equal to endRange with no exclusion is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(
            gateway="10.20.30.200",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            exclusions=[],
        ))
    assert "distribution range" in str(exc_info.value).lower()


def test_gateway_in_range_excluded_by_adjacent_range():
    """Exclusion range covering gateway makes the configuration safe."""
    scope = DhcpScopePayload(**_minimal_scope(
        gateway="10.20.30.105",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        exclusions=[
            {"startAddress": "10.20.30.100", "endAddress": "10.20.30.110"},
        ],
    ))
    assert str(scope.gateway) == "10.20.30.105"


# ---------------------------------------------------------------------------
# PXE boot options (DHCP 66/67) — optional, but both-or-nothing
#
# Three legal states and no fourth. Mirrored against the CI validator's duplicate
# model in tests/test_validate_dhcp_values.py so the two cannot drift apart.
# ---------------------------------------------------------------------------

def test_pxe_omitted_entirely_is_valid():
    """The ordinary no-PXE scope: both fields default to "" and nothing is written."""
    scope = DhcpScopePayload(**_minimal_scope())
    assert scope.nextServer == ""
    assert scope.bootFile == ""


def test_pxe_pair_set_together_is_valid():
    scope = DhcpScopePayload(**_minimal_scope(
        nextServer="boot.lab.local", bootFile="snponly.efi",
    ))
    assert scope.nextServer == "boot.lab.local"
    assert scope.bootFile == "snponly.efi"


def test_next_server_alone_rejected():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(nextServer="boot.lab.local"))
    assert "bootFile is required when nextServer is set" in str(exc_info.value)


def test_boot_file_alone_rejected():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(**_minimal_scope(bootFile="snponly.efi"))
    assert "nextServer is required when bootFile is set" in str(exc_info.value)


def test_pxe_none_normalizes_to_empty_string():
    """null in a values file must land on "" — an explicit null is still "no PXE"."""
    scope = DhcpScopePayload(**_minimal_scope(nextServer=None, bootFile=None))
    assert scope.nextServer == ""
    assert scope.bootFile == ""


def test_next_server_accepts_a_bare_ip():
    """Option 66 is a string field, so an IP is just as valid as a host name."""
    scope = DhcpScopePayload(**_minimal_scope(
        nextServer="10.50.1.20", bootFile="snponly.efi",
    ))
    assert scope.nextServer == "10.50.1.20"


def test_boot_file_accepts_a_url():
    """iPXE HTTP chainloading puts a full URL in option 67 — must not be rejected."""
    scope = DhcpScopePayload(**_minimal_scope(
        nextServer="boot.lab.local", bootFile="http://boot.lab.local/ipxe.efi",
    ))
    assert scope.bootFile == "http://boot.lab.local/ipxe.efi"


def test_boot_file_accepts_a_windows_style_path():
    scope = DhcpScopePayload(**_minimal_scope(
        nextServer="boot.lab.local", bootFile="boot\\x64\\wdsmgfw.efi",
    ))
    assert scope.bootFile == "boot\\x64\\wdsmgfw.efi"


@pytest.mark.parametrize("bad", ["boot .efi", " boot.efi", "boot.efi ", "boot\tefi"])
def test_boot_option_rejects_whitespace(bad):
    """Whitespace is illegal in an option 66/67 string and usually means a typo."""
    with pytest.raises(ValidationError):
        DhcpScopePayload(**_minimal_scope(nextServer="boot.lab.local", bootFile=bad))


def test_boot_option_rejects_control_characters():
    with pytest.raises(ValidationError):
        DhcpScopePayload(**_minimal_scope(
            nextServer="boot.lab.local", bootFile="boot\x00.efi",
        ))


def test_boot_option_rejects_value_over_255_bytes():
    """A DHCP option value is at most 255 octets."""
    with pytest.raises(ValidationError):
        DhcpScopePayload(**_minimal_scope(
            nextServer="boot.lab.local", bootFile="a" * 256,
        ))


def test_pxe_fields_rejected_in_body_are_not_special():
    """Both fields are ordinary body state — they round-trip through body()."""
    scope = DhcpScopePayload(**_minimal_scope(
        nextServer="boot.lab.local", bootFile="snponly.efi",
    ))
    body = scope.body().model_dump(mode="json")
    assert body["nextServer"] == "boot.lab.local"
    assert body["bootFile"] == "snponly.efi"
    assert "scope" not in body
