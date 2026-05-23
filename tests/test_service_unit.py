"""Service layer unit tests for create_scope, delete_scope, and list_scopes.

test_diff.py covers update_scope exhaustively; this file covers the remaining
service functions and the specific PS command sequences they issue.
"""
import pytest
import logging
from unittest.mock import patch

from app.errors import ScopeNotFoundError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.errors import PowerShellError

pytestmark = pytest.mark.asyncio


def _make_scope(**overrides):
    base = dict(
        scopeName="Cluster-A",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[],
        failover=None,
    )
    base.update(overrides)
    return DhcpScopePayload(**base)


def _make_failover(**overrides):
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


# ─── create_scope ─────────────────────────────────────────────────────────────

class TestCreateScope:

    async def test_add_scope_called_when_scope_does_not_exist(self):
        payload = _make_scope()
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            # run_ps sequence: scope_exists check → None (not found), then add, then options
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            out = await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert any("Add-DhcpServerv4Scope" in cmd for cmd in commands)
        assert out is result

    async def test_add_scope_skipped_when_scope_exists(self):
        """If scope already exists, Add-DhcpServerv4Scope must NOT be called."""
        payload = _make_scope()
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            # scope_exists returns truthy → scope exists
            mock_ps.side_effect = [
                {"ScopeId": "10.20.30.0"},  # scope_exists check → scope found
                None,                         # Set-DhcpServerv4OptionValue
            ]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert not any("Add-DhcpServerv4Scope" in cmd for cmd in commands)

    async def test_options_always_set_even_when_scope_exists(self):
        """Set-DhcpServerv4OptionValue must always run, whether scope was added or was already there."""
        payload = _make_scope()
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [
                {"ScopeId": "10.20.30.0"},  # scope_exists: exists
                None,                         # Set-DhcpServerv4OptionValue
            ]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in commands)

    async def test_create_without_gateway_does_not_set_router(self):
        payload = _make_scope(gateway=None)
        result = _make_scope(gateway=None)

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [
                None,  # scope_exists → not found
                None,  # Add-DhcpServerv4Scope
                None,  # Set-DhcpServerv4OptionValue
            ]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        option_cmd = next(
            c.args[0] for c in mock_ps.call_args_list
            if c.args and "Set-DhcpServerv4OptionValue" in c.args[0]
        )
        assert "-DnsServer" in option_cmd
        assert "-Router" not in option_cmd

    async def test_exclusion_commands_issued_for_each_exclusion(self):
        payload = _make_scope(exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
            DhcpExclusion(startAddress="10.20.30.20", endAddress="10.20.30.30"),
        ])
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [
                None,  # scope_exists → not found
                None,  # Add-DhcpServerv4Scope
                None,  # Set-DhcpServerv4OptionValue
                None,  # Add-DhcpServerv4ExclusionRange #1
                None,  # Add-DhcpServerv4ExclusionRange #2
            ]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        excl_cmds = [cmd for cmd in commands if "Add-DhcpServerv4ExclusionRange" in cmd]
        assert len(excl_cmds) == 2

    async def test_failover_replication_issued_when_failover_configured(self):
        payload = _make_scope(failover=_make_failover())
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [
                None,  # scope_exists → not found
                None,  # Add-DhcpServerv4Scope
                None,  # Set-DhcpServerv4OptionValue
                # _setup_failover → Get-DhcpServerv4Failover → not found
                PowerShellError("Get-DhcpServerv4Failover", "not found", 1),
                None,  # Add-DhcpServerv4Failover
                None,  # Invoke-DhcpServerv4FailoverReplication
            ]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in commands)

    async def test_no_failover_commands_when_failover_is_none(self):
        payload = _make_scope(failover=None)
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [None, None, None]  # exists, add, options
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert not any("Failover" in cmd for cmd in commands)

    async def test_returns_assembled_scope_not_raw_ps_output(self):
        """create_scope must return the re-assembled canonical scope, not anything from PS."""
        payload = _make_scope()
        expected = _make_scope(scopeName="From-DHCP-Server")

        with patch("app.services.scope_service.run_ps", return_value=None), \
             patch("app.services.scope_service.assemble_scope_state", return_value=expected):
            from app.services import scope_service
            result = await scope_service.create_scope(payload)

        assert result is expected

    async def test_scope_name_in_add_command(self):
        """Add-DhcpServerv4Scope must include the scope name."""
        payload = _make_scope(scopeName="My Cluster Workers")
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        add_cmd = next((cmd for cmd in commands if "Add-DhcpServerv4Scope" in cmd), None)
        assert add_cmd is not None
        assert "My Cluster Workers" in add_cmd

    async def test_dns_servers_in_options_command(self):
        """Set-DhcpServerv4OptionValue must include both DNS servers."""
        payload = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"])
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        opts_cmd = next((cmd for cmd in commands if "Set-DhcpServerv4OptionValue" in cmd), None)
        assert opts_cmd is not None
        assert "10.0.0.53" in opts_cmd
        assert "10.0.0.54" in opts_cmd

    async def test_add_scope_already_exists_race_converges(self):
        """If another writer creates the scope between exists-check and Add, continue."""
        payload = _make_scope()
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [
                PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
                PowerShellError("Add-DhcpServerv4Scope", "scope already exists", 1),
                None,
            ]
            from app.services import scope_service
            out = await scope_service.create_scope(payload)

        assert out is result
        commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert any("Add-DhcpServerv4Scope" in cmd for cmd in commands)
        assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in commands)

    async def test_add_scope_unrelated_error_still_fails(self):
        payload = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state"):
            mock_ps.side_effect = [
                PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
                PowerShellError("Add-DhcpServerv4Scope", "Access denied", 5),
            ]
            from app.services import scope_service
            with pytest.raises(PowerShellError):
                await scope_service.create_scope(payload)

    async def test_create_scope_logs_scope_id(self, caplog):
        payload = _make_scope()
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result), \
             caplog.at_level(logging.INFO):
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        assert any(getattr(record, "scope_id", None) == "10.20.30.0" for record in caplog.records)

    async def test_create_scope_escapes_scope_name_and_description(self):
        payload = _make_scope(
            scopeName="O'Brien $(Remove-DhcpServerv4Scope)",
            description="desc with ' quote and $dollar and `backtick",
        )
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        add_cmd = next(
            c.args[0] for c in mock_ps.call_args_list
            if c.args and "Add-DhcpServerv4Scope" in c.args[0]
        )
        assert "-Name 'O''Brien $(Remove-DhcpServerv4Scope)'" in add_cmd
        assert "-Description 'desc with '' quote and $dollar and `backtick'" in add_cmd

    async def test_create_scope_escapes_dns_domain(self):
        payload = _make_scope(dnsDomain="lab.local'; Remove-DhcpServerv4Scope")
        result = _make_scope()

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=result):
            mock_ps.side_effect = [None, None, None]
            from app.services import scope_service
            await scope_service.create_scope(payload)

        option_cmd = next(
            c.args[0] for c in mock_ps.call_args_list
            if c.args and "Set-DhcpServerv4OptionValue" in c.args[0]
        )
        assert "-DnsDomain 'lab.local''; Remove-DhcpServerv4Scope'" in option_cmd


# ─── get_scope ────────────────────────────────────────────────────────────────

class TestGetScope:

    async def test_get_scope_success_returns_assembled_scope(self):
        scope = _make_scope()
        with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            from app.services import scope_service
            result = await scope_service.get_scope("10.20.30.0")
        assert result is scope

    async def test_get_scope_not_found_raises_domain_error(self):
        """PS not-found error must be translated to ScopeNotFoundError."""
        with patch(
            "app.services.scope_service.assemble_scope_state",
            side_effect=PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
        ):
            from app.services import scope_service
            with pytest.raises(ScopeNotFoundError) as exc_info:
                await scope_service.get_scope("10.20.30.0")
        assert exc_info.value.scope_id == "10.20.30.0"

    async def test_get_scope_permission_error_propagates_as_ps_error(self):
        """Non-not-found PS errors must not be swallowed — must propagate to the caller."""
        with patch(
            "app.services.scope_service.assemble_scope_state",
            side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access denied", 5),
        ):
            from app.services import scope_service
            with pytest.raises(PowerShellError):
                await scope_service.get_scope("10.20.30.0")


# ─── failover command construction ───────────────────────────────────────────

class TestFailoverCommandConstruction:

    async def test_relationship_name_is_single_quoted(self):
        failover = _make_failover(
            relationshipName="rel'$(evil)",
        )

        with patch("app.services.scope_service.run_ps") as mock_ps:
            from app.services.scope_service import _create_failover_relationship
            await _create_failover_relationship("10.20.30.0", failover)

        cmd = mock_ps.call_args.args[0]
        assert "-Name 'rel''$(evil)'" in cmd


# ─── delete_scope ─────────────────────────────────────────────────────────────

class TestDeleteScope:

    async def test_delete_scope_not_found_is_idempotent(self):
        """If scope doesn't exist, delete must return silently without any PS commands."""
        with patch("app.services.scope_service.run_ps") as mock_ps:
            # scope_exists: run_ps raises not-found
            mock_ps.side_effect = PowerShellError(
                "Get-DhcpServerv4Scope", "No DHCP scope found", 1
            )
            from app.services import scope_service
            await scope_service.delete_scope("10.20.30.0")

        # Only the scope_exists check ran; no delete commands issued
        cmds = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert not any("Remove-DhcpServerv4Scope" in cmd for cmd in cmds)

    async def test_delete_scope_full_flow_no_failover(self):
        """Delete a scope without failover: remove scope only (no failover/exclusion cmds)."""
        scope = _make_scope(exclusions=[], failover=None)

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            mock_ps.side_effect = [
                {"ScopeId": "10.20.30.0"},  # scope_exists → found
                None,                         # Remove-DhcpServerv4Scope
            ]
            from app.services import scope_service
            await scope_service.delete_scope("10.20.30.0")

        cmds = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert any("Remove-DhcpServerv4Scope" in cmd for cmd in cmds)
        assert not any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in cmds)

    async def test_delete_scope_removes_failover_before_scope(self):
        """Failover scope must be detached before the scope itself is removed."""
        scope = _make_scope(
            exclusions=[],
            failover=_make_failover(relationshipName="my-rel"),
        )

        call_order = []

        def track_call(cmd, **kwargs):
            call_order.append(cmd)
            if "Get-DhcpServerv4Failover" in cmd:
                return None  # best_effort call returns None
            return None

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            mock_ps.side_effect = [
                {"ScopeId": "10.20.30.0"},      # scope_exists → found
                None,                             # Remove-DhcpServerv4FailoverScope
                PowerShellError("Get-DhcpServerv4Failover", "not found", 1),  # check remaining
                None,                             # Remove-DhcpServerv4Scope
            ]
            from app.services import scope_service
            await scope_service.delete_scope("10.20.30.0")

        cmds = [c.args[0] for c in mock_ps.call_args_list if c.args]
        # Failover removal must appear before scope removal
        failover_idx = next(i for i, cmd in enumerate(cmds) if "Remove-DhcpServerv4FailoverScope" in cmd)
        scope_idx = next(i for i, cmd in enumerate(cmds) if "Remove-DhcpServerv4Scope" in cmd)
        assert failover_idx < scope_idx

    async def test_delete_scope_removes_exclusions_before_scope(self):
        """Exclusions must be removed before the scope itself is removed."""
        scope = _make_scope(
            exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10")],
            failover=None,
        )

        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            mock_ps.side_effect = [
                {"ScopeId": "10.20.30.0"},  # scope_exists → found
                None,                         # Remove-DhcpServerv4ExclusionRange
                None,                         # Remove-DhcpServerv4Scope
            ]
            from app.services import scope_service
            await scope_service.delete_scope("10.20.30.0")

        cmds = [c.args[0] for c in mock_ps.call_args_list if c.args]
        excl_idx = next(i for i, cmd in enumerate(cmds) if "Remove-DhcpServerv4ExclusionRange" in cmd)
        scope_idx = next(i for i, cmd in enumerate(cmds) if "Remove-DhcpServerv4Scope" in cmd)
        assert excl_idx < scope_idx

    async def test_delete_scope_disappears_between_check_and_assembly(self):
        """Race condition: scope vanishes between scope_exists and assemble → returns silently."""
        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch(
                "app.services.scope_service.assemble_scope_state",
                side_effect=PowerShellError("Get-DhcpServerv4Scope", "not found", 1),
             ):
            mock_ps.return_value = {"ScopeId": "10.20.30.0"}  # scope_exists: found
            from app.services import scope_service
            # Must not raise — scope already gone is acceptable
            await scope_service.delete_scope("10.20.30.0")

    async def test_delete_scope_permission_error_during_assembly_propagates(self):
        """Non-not-found PS error during assembly must NOT produce silent 204.

        Crossplane would receive 204 and remove the CR while the scope remains
        on the DHCP server. The error must propagate so Crossplane retries.
        """
        with patch("app.services.scope_service.run_ps") as mock_ps, \
             patch(
                "app.services.scope_service.assemble_scope_state",
                side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access denied", 5),
             ):
            mock_ps.return_value = {"ScopeId": "10.20.30.0"}  # scope_exists: found
            from app.services import scope_service
            with pytest.raises(PowerShellError):
                await scope_service.delete_scope("10.20.30.0")


# ─── list_scopes ──────────────────────────────────────────────────────────────

class TestListScopes:
    """Tests for list_scopes().

    list_scopes() now uses a single PowerShell process that fetches all scopes
    at once. The PS script emits one {scope, options, exclusions, failover} object
    per scope (the same per-scope structure as get_scope_state).  PowerShell
    collapses a single-element array to a plain object; normalize_list() handles both.

    Returns DhcpScopeListResponse with valid scopes in .scopes and per-scope
    assembly failures in .errors so a single broken scope does not hide the rest.
    """

    async def test_empty_server_returns_empty_lists(self):
        """No scopes → PS returns null → normalize_list → [] → empty response."""
        with patch("app.services.scope_service.run_ps", return_value=None):
            from app.services import scope_service
            result = await scope_service.list_scopes()
        assert result.scopes == []
        assert result.errors == []

    async def test_single_scope_dict_assembled(self):
        """PowerShell collapses a single-scope result to a plain dict (not a list).
        normalize_list wraps it; the scope is assembled via build_payload_from_scope_state.
        """
        single_scope = _make_scope()
        raw_entry = {
            "scope": {"ScopeId": "10.20.30.0"},
            "options": [],
            "exclusions": [],
            "failover": None,
        }

        with patch("app.services.scope_service.run_ps", return_value=raw_entry), \
             patch("app.services.scope_service.build_payload_from_scope_state",
                   return_value=single_scope):
            from app.services import scope_service
            result = await scope_service.list_scopes()

        assert len(result.scopes) == 1
        assert result.errors == []

    async def test_multiple_scopes_sorted_numerically(self):
        """list_scopes must sort by IP integer, not lexicographically.

        '10.20.9.0' < '10.20.30.0' numerically but lexicographic sort would
        put '10.20.30.0' first.  The sorted() key uses ip_to_int.
        """
        scope_9 = _make_scope(
            scopeName="Scope-9", network="10.20.9.0",
            startRange="10.20.9.100", endRange="10.20.9.200", gateway="10.20.9.1",
        )
        scope_30 = _make_scope(scopeName="Scope-30", network="10.20.30.0")

        # PS returns wrong order; list_scopes must sort the result.
        raw_entries = [
            {"scope": {"ScopeId": "10.20.30.0"}, "options": [], "exclusions": [], "failover": None},
            {"scope": {"ScopeId": "10.20.9.0"},  "options": [], "exclusions": [], "failover": None},
        ]

        def fake_build(scope_id, state):
            return scope_9 if scope_id == "10.20.9.0" else scope_30

        with patch("app.services.scope_service.run_ps", return_value=raw_entries), \
             patch("app.services.scope_service.build_payload_from_scope_state",
                   side_effect=fake_build):
            from app.services import scope_service
            result = await scope_service.list_scopes()

        assert len(result.scopes) == 2
        assert str(result.scopes[0].network) == "10.20.9.0"
        assert str(result.scopes[1].network) == "10.20.30.0"

    async def test_entry_without_scope_id_skipped(self):
        """Entries where scope.ScopeId is absent or empty are silently skipped."""
        good_scope = _make_scope()
        raw_entries = [
            {"scope": {"ScopeId": "10.20.30.0"}, "options": [], "exclusions": [], "failover": None},
            {"scope": {},                          "options": [], "exclusions": [], "failover": None},  # no ScopeId
        ]

        with patch("app.services.scope_service.run_ps", return_value=raw_entries), \
             patch("app.services.scope_service.build_payload_from_scope_state",
                   return_value=good_scope):
            from app.services import scope_service
            result = await scope_service.list_scopes()

        assert len(result.scopes) == 1

    async def test_broken_scope_goes_to_errors_others_returned(self):
        """A scope that fails assembly must appear in errors, not crash the whole list."""
        good_scope = _make_scope()
        raw_entries = [
            {"scope": {"ScopeId": "10.20.30.0"}, "options": [], "exclusions": [], "failover": None},
            {"scope": {"ScopeId": "10.20.31.0"}, "options": [], "exclusions": [], "failover": None},
        ]

        def fake_build(scope_id, state):
            if scope_id == "10.20.31.0":
                raise ValueError("DNS servers list is empty")
            return good_scope

        with patch("app.services.scope_service.run_ps", return_value=raw_entries), \
             patch("app.services.scope_service.build_payload_from_scope_state",
                   side_effect=fake_build):
            from app.services import scope_service
            result = await scope_service.list_scopes()

        assert len(result.scopes) == 1
        assert len(result.errors) == 1
        assert result.errors[0].scopeId == "10.20.31.0"
        assert "DNS" in result.errors[0].error

    async def test_missing_dns_scope_in_errors_not_exception(self):
        """DhcpConflictError from missing DNS servers must land in errors, not propagate."""
        from app.errors import DhcpConflictError
        raw_entries = [
            {"scope": {"ScopeId": "10.20.30.0"}, "options": [], "exclusions": [], "failover": None},
        ]

        with patch("app.services.scope_service.run_ps", return_value=raw_entries), \
             patch("app.services.scope_service.build_payload_from_scope_state",
                   side_effect=DhcpConflictError("No DNS servers configured for this scope")):
            from app.services import scope_service
            result = await scope_service.list_scopes()

        assert result.scopes == []
        assert len(result.errors) == 1
        assert result.errors[0].scopeId == "10.20.30.0"
