"""
GET/PUT parity tests — the contract that prevents Crossplane reconciliation loops.

Crossplane provider-http reconciles every ~60 seconds per scope:
  1. GET /api/v1/scopes/{network}
  2. Deep-compare response JSON to stored desired body (from Helm PUT mapping)
  3. Any field difference → issue PUT immediately

If these two JSONs are not identical, Crossplane issues a PUT every 60 seconds forever.
Each PUT triggers real PowerShell commands on the DHCP server.

These tests trace every field from mock PowerShell output → parse → serialize → JSON
and verify it matches exactly what the Helm template would render as the PUT body.
"""
import json
import asyncio
import shutil
import subprocess
import tempfile
import textwrap
from unittest.mock import patch
import pytest
import yaml
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_parsers import assemble_scope_state, parse_failover
from app.utils.ip_utils import parse_timespan_days, parse_timespan_minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope_json(**overrides) -> dict:
    """The canonical desired-state dict — what Helm/Crossplane sends as PUT body.

    Carries no scope address: the identity is in the URL, so the PUT body and the
    GET response are both pure state. That is what makes them directly comparable.
    """
    base = {
        "scopeName": "Cluster-A",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.30.100",
        "endRange": "10.20.30.200",
        "leaseDurationDays": 8,
        "description": "",
        "gateway": "10.20.30.1",
        "dnsServers": ["10.50.1.5", "10.50.1.6"],
        "dnsDomain": "lab.local",
        "nextServer": "",
        "bootFile": "",
        "exclusions": [
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
            {"startAddress": "10.20.30.241", "endAddress": "10.20.30.254"},
        ],
        "failover": None,
    }
    base.update(overrides)
    return base


def _ps_scope(**overrides) -> dict:
    """Mocked Get-DhcpServerv4Scope output."""
    base = {
        "Name": "Cluster-A",
        "ScopeId": "10.20.30.0",
        "SubnetMask": "255.255.255.0",
        "StartRange": "10.20.30.100",
        "EndRange": "10.20.30.200",
        "LeaseDuration": "8.00:00:00",
        "Description": "",
        "State": "Active",
    }
    base.update(overrides)
    return base


def _ps_options(**overrides) -> list:
    """Mocked Get-DhcpServerv4OptionValue output."""
    base = [
        {"OptionId": 3, "Value": ["10.20.30.1"]},
        {"OptionId": 6, "Value": ["10.50.1.5", "10.50.1.6"]},
        {"OptionId": 15, "Value": ["lab.local"]},
    ]
    return overrides.get("options", base)


def _ps_exclusions() -> list:
    return [
        {"StartRange": "10.20.30.1", "EndRange": "10.20.30.10"},
        {"StartRange": "10.20.30.241", "EndRange": "10.20.30.254"},
    ]


def _ps_failover(**overrides) -> dict:
    base = {
        "Name": "tomer-hc-failover",
        "PartnerServer": "dhcp02.lab.local",
        "Mode": "HotStandby",
        "ServerRole": "Active",
        "ReservePercent": 5,
        "LoadBalancePercent": 0,  # canonical: Windows does not use this field for HotStandby
        "MaxClientLeadTime": "1:00:00",
    }
    base.update(overrides)
    return base


def _assemble(scope_raw, options_raw, exclusions_raw, failover_raw=None) -> dict:
    state = {
        "scope": scope_raw,
        "options": options_raw,
        "exclusions": [] if exclusions_raw is None else exclusions_raw,
        "failover": failover_raw,
    }
    with patch("app.services.ps_parsers.run_ps", return_value=state):
        result = asyncio.run(assemble_scope_state("10.20.30.0"))
    # .body() is exactly what the single-scope endpoints return
    return result.body().model_dump(mode="json")


# ---------------------------------------------------------------------------
# Field-by-field parity: scalar fields
# ---------------------------------------------------------------------------

class TestScalarFieldParity:
    """Each test: verify a single field round-trips from PS output to the same
    value Helm/Crossplane would put in the PUT body."""

    def test_scope_name(self):
        got = _assemble(_ps_scope(Name="Cluster-A"), _ps_options(), _ps_exclusions())
        assert got["scopeName"] == "Cluster-A"

    def test_scope_name_with_spaces(self):
        """Scope names with spaces must survive round-trip unchanged."""
        got = _assemble(_ps_scope(Name="My Production Scope"), _ps_options(), _ps_exclusions())
        assert got["scopeName"] == "My Production Scope"

    def test_scope_comes_from_path_not_ps(self):
        """The scope identity is always the address from the URL path, never PS ScopeId.

        It is absent from the wire body entirely, so check the domain model, which
        is where the identity is carried internally.
        """
        state = {
            "scope": _ps_scope(ScopeId="10.20.30.1"),
            "options": _ps_options(),
            "exclusions": _ps_exclusions(),
            "failover": None,
        }
        with patch("app.services.ps_parsers.run_ps", return_value=state):
            result = asyncio.run(assemble_scope_state("10.20.30.0"))
        assert str(result.scope) == "10.20.30.0"  # path value, not PS value
        assert "scope" not in result.body().model_dump(mode="json")

    def test_subnet_mask(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["subnetMask"] == "255.255.255.0"

    def test_start_range(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["startRange"] == "10.20.30.100"

    def test_end_range(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["endRange"] == "10.20.30.200"

    def test_lease_duration_8_days(self):
        got = _assemble(_ps_scope(LeaseDuration="8.00:00:00"), _ps_options(), _ps_exclusions())
        assert got["leaseDurationDays"] == 8
        assert isinstance(got["leaseDurationDays"], int)  # must be int, not float

    def test_lease_duration_1_day(self):
        got = _assemble(_ps_scope(LeaseDuration="1.00:00:00"), _ps_options(), _ps_exclusions())
        assert got["leaseDurationDays"] == 1

    def test_description_empty_string(self):
        """Empty description from PS must serialize as "" not null."""
        got = _assemble(_ps_scope(Description=""), _ps_options(), _ps_exclusions())
        assert got["description"] == ""
        assert got["description"] is not None  # must NOT be null

    def test_description_null_from_ps_normalizes_to_empty_string(self):
        """PS may return null for Description on scopes without one.
        Must normalize to "" to match Helm's 'description: ""' rendering."""
        got = _assemble(_ps_scope(Description=None), _ps_options(), _ps_exclusions())
        assert got["description"] == ""

    def test_description_with_value(self):
        got = _assemble(_ps_scope(Description="prod scope"), _ps_options(), _ps_exclusions())
        assert got["description"] == "prod scope"

    def test_gateway_serializes_as_string_not_object(self):
        """IPv4Address must serialize as plain string for JSON comparison."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["gateway"] == "10.20.30.1"
        assert isinstance(got["gateway"], str)

    def test_dns_domain(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["dnsDomain"] == "lab.local"

    def test_dns_domain_empty(self):
        opts = [
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            {"OptionId": 6, "Value": ["10.50.1.5"]},
        ]  # no option 15
        got = _assemble(_ps_scope(), _ps_options(options=opts), [])
        assert got["dnsDomain"] == ""


# ---------------------------------------------------------------------------
# DNS server order — must be preserved (primary/secondary)
# ---------------------------------------------------------------------------

class TestDnsServerOrder:
    """DNS servers are priority-ordered. Primary must stay first.
    DO NOT sort them — that would break primary/secondary semantics."""

    def test_primary_secondary_order_preserved(self):
        """PS returns DNS in insertion order. Must come back in same order."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["dnsServers"] == ["10.50.1.5", "10.50.1.6"]  # primary first

    def test_three_dns_servers_order_preserved(self):
        opts = [
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            {"OptionId": 6, "Value": ["10.1.1.1", "10.2.2.2", "10.3.3.3"]},
            {"OptionId": 15, "Value": ["lab.local"]},
        ]
        got = _assemble(_ps_scope(), _ps_options(options=opts), [])
        assert got["dnsServers"] == ["10.1.1.1", "10.2.2.2", "10.3.3.3"]

    def test_higher_ip_first_order_preserved(self):
        """Primary DNS can have a higher IP than secondary — order must NOT be sorted.
        Example from values.yaml: dnsServers: [10.2.2.5, 10.2.2.4]
        If the API sorted these, GET would return [10.2.2.4, 10.2.2.5] while the
        Helm PUT body has [10.2.2.5, 10.2.2.4] — causing a PUT loop every 60 seconds."""
        opts = [
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            {"OptionId": 6, "Value": ["10.2.2.5", "10.2.2.4"]},  # higher IP is primary
            {"OptionId": 15, "Value": ["lab.local"]},
        ]
        got = _assemble(_ps_scope(), _ps_options(options=opts), [])
        assert got["dnsServers"] == ["10.2.2.5", "10.2.2.4"], (
            "DNS server order must be preserved exactly as PowerShell returns it. "
            "Sorting would break primary/secondary semantics and cause a Crossplane PUT loop."
        )

    def test_dns_servers_serialize_as_strings(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        for ip in got["dnsServers"]:
            assert isinstance(ip, str), f"DNS server {ip!r} must be a plain string"


# ---------------------------------------------------------------------------
# Exclusions — ordering is critical
# ---------------------------------------------------------------------------

class TestExclusionParity:

    def test_exclusions_sorted_by_ip_ascending(self):
        """GET always sorts exclusions by IP. values.yaml MUST match this order."""
        # PS returns them in reverse order (e.g., manually added in wrong order)
        reversed_excl = [
            {"StartRange": "10.20.30.241", "EndRange": "10.20.30.254"},  # higher IP first
            {"StartRange": "10.20.30.1", "EndRange": "10.20.30.10"},
        ]
        got = _assemble(_ps_scope(), _ps_options(), reversed_excl)
        # GET always returns sorted — lower IP first
        assert got["exclusions"][0]["startAddress"] == "10.20.30.1"
        assert got["exclusions"][1]["startAddress"] == "10.20.30.241"

    def test_exclusions_out_of_order_in_values_causes_mismatch(self):
        """DEMONSTRATES THE RECONCILIATION LOOP BUG.

        If values.yaml has exclusions in non-IP-numerical order,
        the Helm-rendered PUT body will differ from the GET response,
        causing Crossplane to issue a PUT every 60 seconds forever.

        PREVENTION: exclusions in values.yaml MUST be in ascending IP order.
        """
        # What Helm renders if values.yaml has exclusions in WRONG order
        helm_put_body_exclusions = [
            {"startAddress": "10.20.30.241", "endAddress": "10.20.30.254"},  # WRONG: higher first
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
        ]
        # What GET returns (always sorted by IP)
        get_excl = [
            {"StartRange": "10.20.30.241", "EndRange": "10.20.30.254"},
            {"StartRange": "10.20.30.1", "EndRange": "10.20.30.10"},
        ]
        got = _assemble(_ps_scope(), _ps_options(), get_excl)
        get_exclusions = got["exclusions"]

        # This comparison simulates what Crossplane does: deep-equal GET vs PUT body
        assert get_exclusions != helm_put_body_exclusions, (
            "GET returned sorted exclusions but PUT body has different order — "
            "this would cause a Crossplane reconciliation loop"
        )

    def test_exclusions_in_correct_ip_order_matches(self):
        """If values.yaml exclusions are in IP order, GET == PUT body — no loop."""
        helm_put_body_exclusions = [
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},    # CORRECT: lower first
            {"startAddress": "10.20.30.241", "endAddress": "10.20.30.254"},
        ]
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        assert got["exclusions"] == helm_put_body_exclusions

    def test_no_exclusions_serializes_as_empty_list(self):
        got = _assemble(_ps_scope(), _ps_options(), None)
        assert got["exclusions"] == []

    def test_single_exclusion_not_wrapped_in_extra_list(self):
        single = [{"StartRange": "10.20.30.1", "EndRange": "10.20.30.10"}]
        got = _assemble(_ps_scope(), _ps_options(), single)
        assert len(got["exclusions"]) == 1
        assert got["exclusions"][0]["startAddress"] == "10.20.30.1"

    def test_exclusion_addresses_serialize_as_strings(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions())
        for excl in got["exclusions"]:
            assert isinstance(excl["startAddress"], str)
            assert isinstance(excl["endAddress"], str)


# ---------------------------------------------------------------------------
# Failover field parity
# ---------------------------------------------------------------------------

class TestFailoverParity:

    def test_failover_null_when_not_configured(self):
        """No failover → must serialize as null, matching Helm's 'failover: null'."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=None)
        assert got["failover"] is None

    def test_failover_all_fields_present(self):
        """All observed failover fields must be present."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=_ps_failover())
        f = got["failover"]
        required_fields = {
            "partnerServer", "relationshipName", "mode", "serverRole",
            "reservePercent", "loadBalancePercent", "maxClientLeadTimeMinutes",
        }
        assert required_fields == set(f.keys()), (
            f"Missing failover fields: {required_fields - set(f.keys())}"
        )

    def test_failover_partner_server(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=_ps_failover())
        assert got["failover"]["partnerServer"] == "dhcp02.lab.local"

    def test_failover_relationship_name(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=_ps_failover())
        assert got["failover"]["relationshipName"] == "tomer-hc-failover"

    def test_failover_mode_hotstandby(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(Mode="HotStandby"))
        assert got["failover"]["mode"] == "HotStandby"

    def test_failover_mode_loadbalance(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(Mode="LoadBalance"))
        assert got["failover"]["mode"] == "LoadBalance"

    def test_failover_server_role(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=_ps_failover())
        assert got["failover"]["serverRole"] == "Active"

    def test_failover_reserve_percent_is_int(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(ReservePercent=5))
        assert got["failover"]["reservePercent"] == 5
        assert isinstance(got["failover"]["reservePercent"], int)

    def test_failover_load_balance_percent_loadbalance_mode(self):
        """LoadBalance mode: loadBalancePercent comes from PS output, preserved as-is."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(Mode="LoadBalance", LoadBalancePercent=60))
        assert got["failover"]["loadBalancePercent"] == 60
        assert isinstance(got["failover"]["loadBalancePercent"], int)

    def test_hotstandby_load_balance_percent_normalized_to_zero(self):
        """HotStandby mode: loadBalancePercent must always be 0 regardless of PS output.
        Windows may return any value here for HotStandby; we normalize to 0 to prevent
        a GET/PUT mismatch against the Helm template which renders loadBalancePercent: 0."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(Mode="HotStandby", LoadBalancePercent=50))
        assert got["failover"]["loadBalancePercent"] == 0

    def test_failover_max_lead_time_hh_mm_ss(self):
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(MaxClientLeadTime="1:00:00"))
        assert got["failover"]["maxClientLeadTimeMinutes"] == 60

    def test_failover_max_lead_time_day_format_not_crash(self):
        """PS emits 'd.HH:MM:SS' for values >= 24h. Must not crash.
        This was a real production bug: parse_timespan_minutes crashed with ValueError."""
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(),
                        failover_raw=_ps_failover(MaxClientLeadTime="1.00:00:00"))
        assert got["failover"]["maxClientLeadTimeMinutes"] == 1440

# ---------------------------------------------------------------------------
# Full end-to-end parity: GET response must equal PUT body
# ---------------------------------------------------------------------------

class TestFullPayloadParity:

    def test_full_scope_without_failover(self):
        """Complete scope with no failover — every field checked."""
        desired = _scope_json(failover=None)
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=None)
        assert got == desired, (
            f"GET/PUT mismatch!\n"
            f"PUT body: {json.dumps(desired, indent=2)}\n"
            f"GET resp: {json.dumps(got, indent=2)}"
        )

    def test_full_scope_with_failover(self):
        """Complete scope with failover — all 8 failover fields checked."""
        desired = _scope_json(failover={
            "partnerServer": "dhcp02.lab.local",
            "relationshipName": "tomer-hc-failover",
            "mode": "HotStandby",
            "serverRole": "Active",
            "reservePercent": 5,
            "loadBalancePercent": 0,  # HotStandby: always 0 (Helm renders 0, GET normalizes to 0)
            "maxClientLeadTimeMinutes": 60,
        })
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=_ps_failover())
        assert got == desired, (
            f"GET/PUT mismatch with failover!\n"
            f"PUT body: {json.dumps(desired, indent=2)}\n"
            f"GET resp: {json.dumps(got, indent=2)}"
        )

    def test_full_scope_with_loadbalance_failover(self):
        """Complete scope with LoadBalance failover — full GET/PUT parity.

        serverRole must be 'Active' (normalised), reservePercent must be 0 (normalised).
        loadBalancePercent comes from PS output.  These invariants prevent a Crossplane
        reconciliation loop when the Helm template renders the canonical LoadBalance shape.
        """
        desired = _scope_json(failover={
            "partnerServer": "dhcp02.lab.local",
            "relationshipName": "tomer-hc-failover",
            "mode": "LoadBalance",
            "serverRole": "Active",      # normalised for LoadBalance
            "reservePercent": 0,          # normalised for LoadBalance
            "loadBalancePercent": 50,
            "maxClientLeadTimeMinutes": 60,
        })
        ps_fo = _ps_failover(
            Mode="LoadBalance",
            ServerRole="Active",
            ReservePercent=0,
            LoadBalancePercent=50,
        )
        got = _assemble(_ps_scope(), _ps_options(), _ps_exclusions(), failover_raw=ps_fo)
        assert got == desired, (
            f"GET/PUT mismatch with LoadBalance failover!\n"
            f"PUT body: {json.dumps(desired, indent=2)}\n"
            f"GET resp: {json.dumps(got, indent=2)}"
        )

    def test_scope_with_empty_description_no_loop(self):
        """Scope where description was never set — PS returns null — must not cause loop."""
        desired = _scope_json(description="", failover=None)
        got = _assemble(_ps_scope(Description=None), _ps_options(), _ps_exclusions())
        assert got["description"] == desired["description"]

    def test_scope_with_no_exclusions_no_loop(self):
        desired = _scope_json(exclusions=[], failover=None)
        got = _assemble(_ps_scope(), _ps_options(), None)
        assert got["exclusions"] == desired["exclusions"]


# ---------------------------------------------------------------------------
# Derived defaults — the end-to-end proof that omitting keys cannot loop
# ---------------------------------------------------------------------------

class TestDerivedDefaultParity:
    """subnetMask and gateway may be omitted from a values file and derived.

    Deriving them in only one place would be a reconciliation bug: GET always
    reports the concrete values the DHCP server holds, so if the rendered PUT body
    said null (or omitted the key), Crossplane would see a permanent diff and PUT
    every 60 seconds forever. These tests render the *actual* chart and compare it
    to the *actual* GET assembly — the two independent implementations of the rule.
    """

    _VALUES = textwrap.dedent("""\
        apiServer:
          url: https://dhcp-api.lab.local
          tokenSecretRef: null
        dhcp_values:
          scopeName: "Cluster-A"
          network: "10.20.30.0"
          startRange: "10.20.30.100"
          endRange: "10.20.30.200"
          leaseDurationDays: 8
          description: ""
          dns:
            servers:
              - "10.50.1.5"
              - "10.50.1.6"
            domain: "lab.local"
          exclusions:
            - startAddress: "10.20.30.1"
              endAddress: "10.20.30.10"
            - startAddress: "10.20.30.241"
              endAddress: "10.20.30.254"
          failover: null
    """)

    @staticmethod
    def _rendered_body(values: str) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
            fh.write(values)
            path = fh.name
        result = subprocess.run(
            ["helm", "template", "parity", "helm", "-f", path],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        cr = next(iter(yaml.safe_load_all(result.stdout)))
        return cr["spec"]["forProvider"]["payload"]["body"]

    @pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not available")
    def test_rendered_defaults_match_get_response(self):
        """The whole point: rendered body == GET response, byte for byte."""
        desired = self._rendered_body(self._VALUES)
        # What the DHCP server holds after the API applied that body.
        got = _assemble(
            _ps_scope(Name="Cluster-A", SubnetMask="255.255.255.0"),
            _ps_options(options=[
                {"OptionId": 3, "Value": ["10.20.30.254"]},
                {"OptionId": 6, "Value": ["10.50.1.5", "10.50.1.6"]},
                {"OptionId": 15, "Value": ["lab.local"]},
            ]),
            _ps_exclusions(),
        )
        assert got == desired, (
            f"GET/PUT mismatch on derived defaults!\n"
            f"PUT body: {json.dumps(desired, indent=2)}\n"
            f"GET resp: {json.dumps(got, indent=2)}"
        )

    @pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not available")
    def test_rendered_empty_gateway_matches_scope_without_option_3(self):
        """The no-gateway path must stay loop-free too.

        gateway: "" renders null, and a scope with no DHCP option 3 reads back as
        null — so the pair still agrees.
        """
        desired = self._rendered_body(self._VALUES + '  gateway: ""\n')
        got = _assemble(
            _ps_scope(Name="Cluster-A"),
            _ps_options(options=[
                {"OptionId": 6, "Value": ["10.50.1.5", "10.50.1.6"]},
                {"OptionId": 15, "Value": ["lab.local"]},
            ]),
            _ps_exclusions(),
        )
        assert desired["gateway"] is None
        assert got == desired

    def test_api_derives_the_same_address_helm_does(self):
        """Both implementations of the .254 rule agree, without needing helm."""
        payload = DhcpScopePayload(
            scope="10.20.30.0",
            scopeName="Cluster-A",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            dnsServers=["10.50.1.5"],
        )
        assert str(payload.subnetMask) == "255.255.255.0"
        assert str(payload.gateway) == "10.20.30.254"
