import pytest
from unittest.mock import patch
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload


@pytest.fixture(autouse=True)
def _bypass_dhcp_service_validation():
    """Bypass DHCP environment validation for all tests.

    Tests that specifically test validation behaviour (test_dhcp_service.py) call
    _reset_validation_cache() themselves and set up their own mocks — this
    autouse fixture does not interfere because an inner ``with patch(...)``
    overrides the outer one for the duration of that context.

    Without this fixture all existing tests would fail on Linux / WSL / macOS
    because run_ps() and the scopes router dependency both call
    validate_dhcp_environment() which would immediately raise DhcpEnvironmentError.
    """
    from app.services import dhcp_service
    dhcp_service._reset_validation_cache()
    with patch("app.services.dhcp_service.validate_dhcp_environment"):
        yield
    dhcp_service._reset_validation_cache()


@pytest.fixture
def sample_failover():
    return DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        maxClientLeadTimeMinutes=60,
    )


@pytest.fixture
def sample_scope_payload(sample_failover):
    return DhcpScopePayload(
        scopeName="Cluster-A Management",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99"),
        ],
        failover=sample_failover,
    )


@pytest.fixture
def sample_scope_payload_no_failover():
    return DhcpScopePayload(
        scopeName="Cluster-A Management",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99"),
        ],
        failover=None,
    )


# ---------------------------------------------------------------------------
# Mock PowerShell output fixtures (mimic ConvertTo-Json output)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ps_scope_raw():
    return {
        "Name": "Cluster-A Management",
        "SubnetMask": "255.255.255.0",
        "StartRange": "10.20.30.100",
        "EndRange": "10.20.30.200",
        "LeaseDuration": "8.00:00:00",
        "Description": "Cluster A management network",
        "State": "Active",
        "ScopeId": "10.20.30.0",
    }


@pytest.fixture
def mock_ps_options_raw():
    return [
        {"OptionId": 3, "Value": ["10.20.30.1"], "Name": "Router"},
        {"OptionId": 6, "Value": ["10.0.0.53", "10.0.0.54"], "Name": "DNS Servers"},
        {"OptionId": 15, "Value": ["lab.local"], "Name": "DNS Domain Name"},
    ]


@pytest.fixture
def mock_ps_exclusions_raw():
    return [
        {"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"},
    ]


@pytest.fixture
def mock_ps_failover_raw():
    return {
        "Name": "mce1-failover",
        "PartnerServer": "dhcp02.lab.local",
        "Mode": "HotStandby",
        "ServerRole": "Active",
        "ReservePercent": 5,
        "LoadBalancePercent": 0,  # canonical: Windows does not use this field for HotStandby
        "MaxClientLeadTime": "1:00:00",
    }
