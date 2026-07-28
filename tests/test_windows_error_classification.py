"""Regression tests anchored in REAL Windows DHCP error output.

Every string in this file was captured verbatim from Windows Server 2022
(21H2, build 20348.5386, DHCP server version 10.0) on 2026-07-28, not invented.

That distinction is the whole point. The pre-existing unit suite classified
errors correctly against made-up messages like "The scope was not found" and so
never noticed that the DhcpServer module says no such thing. A missing scope
actually reports:

    Failed to get scope information for scope 10.20.30.0 on DHCP server HOST.
        + CategoryInfo          : ObjectNotFound
        + FullyQualifiedErrorId : DHCP 20005,Get-DhcpServerv4Scope

which contains no not-found wording at all. The consequence was a 500 where the
contract requires 404, so Crossplane never learned to POST and reconciliation
could not converge (CLAUDE.md sections 5 and 9).
"""
from __future__ import annotations

import pytest

from app.models import DhcpScopePayload
from app.services.ps_executor import is_already_exists_error, is_not_found_error
from app.services.psrp_pool import _format_error_record
from app.services.scope_service import _set_options_command
from app.utils.powershell import ps_ipv4

HOST = "EC2AMAZ-VM9EKPL"


def _stderr(message: str, category: str, fq_error: str) -> str:
    """Reproduce console PowerShell's stderr layout."""
    return (
        f"{message}\n"
        f"    + CategoryInfo          : {category}\n"
        f"    + FullyQualifiedErrorId : {fq_error}"
    )


# ─── Not-found classification ─────────────────────────────────────────────────

class TestNotFoundAgainstRealOutput:

    def test_missing_scope_on_get(self):
        stderr = _stderr(
            f"Failed to get scope information for scope 10.20.30.0 on DHCP server {HOST}.",
            "ObjectNotFound",
            "DHCP 20005,Get-DhcpServerv4Scope",
        )
        assert is_not_found_error(stderr), "GET on a missing scope must yield 404, not 500"

    def test_missing_scope_on_remove(self):
        stderr = _stderr(
            f"Failed to get scope information for scope 10.77.77.0 on DHCP server {HOST}.",
            "ObjectNotFound",
            "DHCP 20005,Remove-DhcpServerv4Scope",
        )
        assert is_not_found_error(stderr), "DELETE must stay idempotent (204) for an absent scope"

    def test_option_value_not_set(self):
        stderr = _stderr(
            f"Failed to get option value of 6 on DHCP server {HOST}.",
            "ObjectNotFound",
            "DHCP 20010,Get-DhcpServerv4OptionValue",
        )
        assert is_not_found_error(stderr)

    def test_no_failover_relationship(self):
        stderr = _stderr(
            f"Failed to get failover relationship for scope 10.66.66.0 on DHCP server {HOST}.",
            "ObjectNotFound",
            "DHCP 20116,Get-DhcpServerv4Failover",
        )
        assert is_not_found_error(stderr)

    def test_bare_message_without_metadata_is_the_regression(self):
        """The message alone carries no not-found signal — the category does.

        Guards the transport-parity requirement: a transport that drops
        CategoryInfo silently reintroduces the 500-instead-of-404 bug.
        """
        bare = f"Failed to get scope information for scope 10.20.30.0 on DHCP server {HOST}."
        assert not is_not_found_error(bare)

    @pytest.mark.parametrize("stderr", [
        "Access is denied.",
        _stderr("Failed to add scope on DHCP server X.", "PermissionDenied",
                "DHCP 20000,Add-DhcpServerv4Scope"),
        "The RPC server is unavailable",
        "",
    ])
    def test_unrelated_failures_are_not_swallowed(self, stderr):
        assert not is_not_found_error(stderr)


# ─── Already-exists classification ────────────────────────────────────────────

class TestAlreadyExistsAgainstRealOutput:

    def test_duplicate_scope(self):
        stderr = _stderr(
            f"Failed to add scope 10.66.66.0 on DHCP server {HOST}.",
            "ResourceExists",
            "DHCP 20052,Add-DhcpServerv4Scope",
        )
        assert is_already_exists_error(stderr), "POST must converge, not fail, when the scope exists"

    def test_duplicate_exclusion_range(self):
        """Category is InvalidData here, so the DHCP code is the only signal."""
        stderr = _stderr(
            "Failed to add exclusion range with start range 10.55.55.60 and end range "
            f"10.55.55.70 to scope 10.55.55.0 on DHCP server {HOST}.",
            "InvalidData",
            "DHCP 20023,Add-DhcpServerv4ExclusionRange",
        )
        assert is_already_exists_error(stderr), "re-POST must be idempotent (CLAUDE.md section 2)"

    def test_generic_invalid_data_is_not_treated_as_already_exists(self):
        """InvalidData alone is too broad — only DHCP 20023 means duplicate."""
        stderr = _stderr(
            "Failed to set something on DHCP server X.",
            "InvalidData",
            "DHCP 29999,Set-DhcpServerv4Something",
        )
        assert not is_already_exists_error(stderr)


# ─── PSRP transport parity ────────────────────────────────────────────────────

class _FakeErrorRecord:
    def __init__(self, message, category, fq_error):
        self.message = message
        self.category = category
        self.fq_error = fq_error


class TestPsrpErrorFormatting:
    """PSRP must reproduce the metadata console PowerShell writes to stderr."""

    def test_category_and_fqeid_are_emitted(self):
        rec = _FakeErrorRecord(
            f"Failed to get scope information for scope 10.20.30.0 on DHCP server {HOST}.",
            13,  # ObjectNotFound
            "DHCP 20005,Get-DhcpServerv4Scope",
        )
        out = _format_error_record(rec)
        assert "ObjectNotFound" in out
        assert "DHCP 20005,Get-DhcpServerv4Scope" in out
        assert "Failed to get scope information" in out

    def test_formatted_record_classifies_as_not_found(self):
        """The end-to-end guarantee: PSRP errors classify like local ones."""
        rec = _FakeErrorRecord("Failed to get scope information for scope X.", 13,
                               "DHCP 20005,Get-DhcpServerv4Scope")
        assert is_not_found_error(_format_error_record(rec))

    def test_resource_exists_category_maps_by_name(self):
        rec = _FakeErrorRecord("Failed to add scope.", 20, "DHCP 20052,Add-DhcpServerv4Scope")
        out = _format_error_record(rec)
        assert "ResourceExists" in out
        assert is_already_exists_error(out)

    def test_unknown_category_falls_back_to_number(self):
        rec = _FakeErrorRecord("Something failed.", 999, "DHCP 1,Some-Cmdlet")
        assert "999" in _format_error_record(rec)

    def test_missing_attributes_do_not_raise(self):
        class Bare:
            pass

        assert isinstance(_format_error_record(Bare()), str)


# ─── -Force on the option command ─────────────────────────────────────────────

class TestSetOptionsForce:
    """Set-DhcpServerv4OptionValue validates DNS servers and rejects unreachable ones.

    Without -Force, "10.10.1.5 is not a valid DNS server" aborts scope creation
    before exclusions are applied. Desired state comes from Git and is
    authoritative, so the API applies it rather than revalidating reachability.
    """

    def _payload(self, **overrides):
        base = dict(
            scopeName="cluster-a",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.50",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.10.1.5", "10.10.1.6"],
            dnsDomain="lab.local",
            exclusions=[],
            failover=None,
        )
        base.update(overrides)
        return DhcpScopePayload(**base)

    def test_force_is_present(self):
        cmd = _set_options_command(ps_ipv4("10.20.30.0"), self._payload())
        assert cmd.rstrip().endswith("-Force"), cmd

    def test_force_present_without_gateway(self):
        cmd = _set_options_command(ps_ipv4("10.20.30.0"), self._payload(gateway=None))
        assert "-Force" in cmd
        assert "-Router" not in cmd

    def test_dns_servers_still_ordered(self):
        """-Force must not disturb primary/secondary ordering."""
        cmd = _set_options_command(ps_ipv4("10.20.30.0"), self._payload())
        assert cmd.index("10.10.1.5") < cmd.index("10.10.1.6")
