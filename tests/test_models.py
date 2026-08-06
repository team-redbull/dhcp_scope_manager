import json
import pytest
from pydantic import ValidationError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload


def test_scope_payload_field_ordering(sample_scope_payload):
    data = sample_scope_payload.model_dump(mode="json")
    keys = list(data.keys())
    expected_keys = [
        "scope", "scopeName", "subnetMask", "startRange", "endRange",
        "leaseDurationDays", "description", "gateway", "dnsServers",
        "dnsDomain", "nextServer", "bootFile", "exclusions", "failover",
    ]
    assert keys == expected_keys


def test_empty_exclusions_not_null():
    payload = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.254", leaseDurationDays=8,
        description="", gateway="10.0.0.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    data = payload.model_dump(mode="json")
    assert data["exclusions"] == []
    assert data["exclusions"] is not None


def test_null_failover_is_none(sample_scope_payload_no_failover):
    data = sample_scope_payload_no_failover.model_dump(mode="json")
    assert data["failover"] is None
    serialized = json.dumps(data)
    assert '"failover": null' in serialized


def test_dns_servers_order_preserved(sample_scope_payload):
    data = sample_scope_payload.model_dump(mode="json")
    assert data["dnsServers"] == ["10.0.0.53", "10.0.0.54"]


def test_lease_duration_is_int(sample_scope_payload):
    data = sample_scope_payload.model_dump(mode="json")
    assert isinstance(data["leaseDurationDays"], int)
    assert data["leaseDurationDays"] == 8


def test_dns_servers_empty_rejected():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
            startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
            description="", gateway="10.0.0.1", dnsServers=[], dnsDomain="",
            exclusions=[], failover=None,
        )


def test_dns_servers_single_server_accepted():
    payload = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
        description="", gateway="10.0.0.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    assert [str(ip) for ip in payload.dnsServers] == ["10.0.0.53"]


def test_dns_servers_multiple_servers_accepted():
    payload = DhcpScopePayload(
        scopeName="Test", scope="10.0.0.0", subnetMask="255.255.255.0",
        startRange="10.0.0.2", endRange="10.0.0.10", leaseDurationDays=8,
        description="", gateway="10.0.0.1",
        dnsServers=["10.0.0.53", "10.0.0.54"], dnsDomain="",
        exclusions=[], failover=None,
    )
    assert [str(ip) for ip in payload.dnsServers] == ["10.0.0.53", "10.0.0.54"]
