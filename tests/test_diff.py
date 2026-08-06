"""Tests for the PUT diff logic in scope_service.update_scope."""
from unittest.mock import patch
import pytest
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.errors import PowerShellError

pytestmark = pytest.mark.asyncio


def _make_scope(**overrides):
    defaults = dict(
        scopeName="Cluster-A",
        scope="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="desc",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=None,
    )
    defaults.update(overrides)
    return DhcpScopePayload(**defaults)


async def _run_update(current_scope, desired_scope):
    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        # First call returns current, second call returns "fresh" state after update
        mock_assemble.side_effect = [current_scope, desired_scope]
        await scope_service.update_scope(current_scope.scope, desired_scope)
        return mock_ps.call_args_list


async def test_no_op_when_identical():
    scope = _make_scope()
    calls = await _run_update(scope, scope)
    assert calls == [], "No PowerShell calls expected when desired == current"


async def test_scope_name_changed():
    current = _make_scope(scopeName="Old Name")
    desired = _make_scope(scopeName="New Name")
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


async def test_lease_changed():
    current = _make_scope(leaseDurationDays=8)
    desired = _make_scope(leaseDurationDays=14)
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


async def test_description_changed():
    current = _make_scope(description="old")
    desired = _make_scope(description="new")
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


async def test_gateway_changed():
    current = _make_scope(gateway="10.20.30.1")
    desired = _make_scope(gateway="10.20.30.2")
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)
    assert any("-Router '10.20.30.2'" in cmd for cmd in ps_commands)


async def test_gateway_removed():
    current = _make_scope(gateway="10.20.30.1")
    desired = _make_scope(gateway=None)
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Remove-DhcpServerv4OptionValue" in cmd and "-OptionId 3" in cmd for cmd in ps_commands)


async def test_dns_changed_without_gateway_does_not_set_router():
    current = _make_scope(gateway=None, dnsServers=["10.0.0.53"])
    desired = _make_scope(gateway=None, dnsServers=["10.0.0.53", "10.0.0.54"])
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    set_cmd = next(cmd for cmd in ps_commands if "Set-DhcpServerv4OptionValue" in cmd)
    assert "-DnsServer" in set_cmd
    assert "-Router" not in set_cmd


async def test_dns_changed():
    current = _make_scope(dnsServers=["10.0.0.53"])
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"])
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


async def test_dns_changed_with_failover_triggers_replication():
    failover = _make_failover()
    current = _make_scope(dnsServers=["10.0.0.53"], failover=failover)
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"], failover=_make_failover())
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


async def test_exclusion_added():
    current = _make_scope(exclusions=[])
    desired = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Add-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


async def test_exclusion_changed_with_failover_triggers_replication():
    current = _make_scope(exclusions=[], failover=_make_failover())
    desired = _make_scope(
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=_make_failover(),
    )
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Add-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


async def test_exclusion_removed():
    current = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    desired = _make_scope(exclusions=[])
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Remove-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


def _make_failover(**overrides):
    defaults = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        maxClientLeadTimeMinutes=60,
    )
    defaults.update(overrides)
    return DhcpFailover(**defaults)


async def test_failover_add_new_relationship():
    """current=None, desired=failover, relationship doesn't exist → Add-DhcpServerv4Failover"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # First run_ps in _setup_failover (Get-DhcpServerv4Failover) raises → new relationship
        mock_ps.side_effect = [
            None,  # _fetch_failover_by_name → no existing relationship
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


async def test_lease_changed_with_failover_triggers_replication():
    current = _make_scope(leaseDurationDays=8, failover=_make_failover())
    desired = _make_scope(leaseDurationDays=14, failover=_make_failover())
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


async def test_failover_add_existing_relationship():
    """current=None, desired=failover, relationship exists → Add-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # Get-DhcpServerv4Failover returns existing relationship
        mock_ps.side_effect = [
            {"Name": "mce1-failover", "ScopeId": "10.20.20.0"},  # existing relationship
            None,  # Add-DhcpServerv4FailoverScope
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


async def test_failover_remove():
    """current=failover, desired=None → Remove-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=_make_failover())
    desired = _make_scope(failover=None)

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.side_effect = [
            None,  # Remove-DhcpServerv4FailoverScope
            None,  # _fetch_failover_by_name → relationship gone → None → no Remove-Failover
        ]
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


async def test_failover_params_updated():
    """Failover params changed → Set-DhcpServerv4Failover + Replication"""
    current = _make_scope(failover=_make_failover(reservePercent=5))
    desired = _make_scope(failover=_make_failover(reservePercent=10))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


async def test_failover_unchanged_no_calls():
    """Identical failover config → no failover cmdlets at all"""
    failover = _make_failover()
    current = _make_scope(failover=failover)
    desired = _make_scope(failover=_make_failover())  # same values, new object

    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    failover_cmds = [c for c in ps_commands if "Failover" in c]
    assert failover_cmds == []


async def test_failover_relationship_name_change_triggers_recreate():
    """Changing relationshipName is an identity change — must remove + recreate, not Set."""
    current = _make_scope(failover=_make_failover(relationshipName="old-rel"))
    desired = _make_scope(failover=_make_failover(relationshipName="new-rel"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    # Must remove from old relationship
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    # Must NOT use Set-DhcpServerv4Failover (not a rename operation)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


async def test_failover_partner_server_change_triggers_recreate():
    """Changing partnerServer is an identity change — must remove + recreate."""
    current = _make_scope(failover=_make_failover(partnerServer="dhcp01.lab.local"))
    desired = _make_scope(failover=_make_failover(partnerServer="dhcp02.lab.local"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope(current.scope, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


async def test_scope_exists_reraises_on_permission_error():
    """scope_exists must not return False on permission errors — it must propagate."""
    from app.services.scope_service import scope_exists
    from app.errors import PowerShellError

    with patch(
        "app.services.scope_service.run_ps",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access is denied", 1),
    ):
        with pytest.raises(PowerShellError):
            await scope_exists("10.20.30.0")


async def test_scope_exists_returns_false_on_not_found():
    """scope_exists returns False when the aggregate scope filter finds no match."""
    from app.services.scope_service import scope_exists

    with patch("app.services.scope_service.run_ps", return_value=False):
        assert await scope_exists("10.20.30.0") is False


# ---------------------------------------------------------------------------
# Failover mode-specific PS command correctness
# ---------------------------------------------------------------------------

async def test_create_failover_loadbalance_excludes_server_role():
    """Add-DhcpServerv4Failover for LoadBalance must NOT include -ServerRole.

    The Windows DHCP cmdlet does not accept -ServerRole for LoadBalance mode.
    Passing it would either cause a cmdlet error or silently corrupt the relationship.
    """
    from app.services import scope_service

    desired = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # not set by caller — model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))

    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [_make_scope(failover=None), desired]
        mock_ps.side_effect = [
            None,  # _fetch_failover_by_name → no existing relationship
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        await scope_service.update_scope("10.20.30.0", desired)

    add_cmd = next(
        c.args[0] for c in mock_ps.call_args_list
        if c.args and "Add-DhcpServerv4Failover" in c.args[0]
    )
    assert "-ServerRole" not in add_cmd, (
        "Add-DhcpServerv4Failover for LoadBalance must not include -ServerRole"
    )
    assert "-LoadBalancePercent 50" in add_cmd
    assert "-Mode" not in add_cmd, (
        "Add-DhcpServerv4Failover has no -Mode parameter; mode comes from the parameter set"
    )


async def test_create_failover_hotstandby_includes_server_role():
    """Add-DhcpServerv4Failover for HotStandby must include -ServerRole and -ReservePercent."""
    from app.services import scope_service

    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=5,
    ))

    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [_make_scope(failover=None), desired]
        mock_ps.side_effect = [
            None,  # _fetch_failover_by_name → no existing relationship
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        await scope_service.update_scope("10.20.30.0", desired)

    add_cmd = next(
        c.args[0] for c in mock_ps.call_args_list
        if c.args and "Add-DhcpServerv4Failover" in c.args[0]
    )
    assert "-ServerRole Active" in add_cmd
    assert "-ReservePercent 5" in add_cmd
    assert "-LoadBalancePercent" not in add_cmd
    assert "-Mode" not in add_cmd, (
        "Add-DhcpServerv4Failover has no -Mode parameter; mode comes from the parameter set"
    )


# ---------------------------------------------------------------------------
# Mode switch: always remove + recreate (never Set)
# ---------------------------------------------------------------------------

async def test_failover_mode_switch_hotstandby_to_loadbalance_triggers_recreate():
    """Switching from HotStandby to LoadBalance must remove + recreate, not Set.

    Set-DhcpServerv4Failover cannot safely handle mode transitions: it does not
    accept -ServerRole, leaving role semantics undefined after the switch.
    """
    current = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=5,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands), (
        "Mode switch must remove scope from old relationship first"
    )
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands), (
        "Mode switch must not use Set-DhcpServerv4Failover"
    )


async def test_failover_mode_switch_loadbalance_to_hotstandby_triggers_recreate():
    """Switching from LoadBalance to HotStandby must remove + recreate, not Set.

    When both current and desired serverRole are 'Active' (LoadBalance normalises
    to Active, HotStandby-Active is explicitly Active), the old identity check
    would not detect a change.  The mode field must be compared explicitly.
    """
    current = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=10,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands), (
        "Mode switch must remove scope from old relationship first"
    )
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands), (
        "Mode switch must not use Set-DhcpServerv4Failover"
    )


async def test_failover_mode_switch_loadbalance_to_hotstandby_standby_role_triggers_recreate():
    """LoadBalance→HotStandby(Standby): serverRole also changes — must remove + recreate."""
    current = _make_scope(failover=_make_failover(
        mode="LoadBalance", serverRole=None, loadBalancePercent=50, reservePercent=0,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Standby", reservePercent=5,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


# ---------------------------------------------------------------------------
# Same-mode mutable updates: only mode-relevant fields trigger Set
# ---------------------------------------------------------------------------

def _make_lb_failover(**overrides):
    """Minimal valid LoadBalance failover."""
    base = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="LoadBalance",
        loadBalancePercent=50,
        maxClientLeadTimeMinutes=60,
    )
    base.update(overrides)
    return DhcpFailover(**base)


async def test_loadbalance_percent_change_triggers_set():
    """Changing loadBalancePercent within LoadBalance must use Set-DhcpServerv4Failover."""
    current = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=70))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    set_cmd = next(c for c in ps_commands if "Set-DhcpServerv4Failover" in c)
    assert "-LoadBalancePercent 70" in set_cmd
    assert "-ReservePercent" not in set_cmd
    assert "-ServerRole" not in set_cmd


async def test_loadbalance_unchanged_no_calls():
    """Identical LoadBalance config (including normalized fields) must produce no cmdlets."""
    failover = _make_lb_failover(loadBalancePercent=50)
    current = _make_scope(failover=failover)
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))

    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    assert not any("Failover" in cmd for cmd in ps_commands), (
        "No failover cmdlets expected when LoadBalance config is unchanged"
    )


async def test_hotstandby_role_change_triggers_recreate():
    """Changing serverRole within HotStandby is identity-level — must remove + recreate."""
    current = _make_scope(failover=_make_failover(mode="HotStandby", serverRole="Active"))
    desired = _make_scope(failover=_make_failover(mode="HotStandby", serverRole="Standby"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        await scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


async def test_normalized_fields_do_not_trigger_spurious_update():
    """Normalized cross-mode fields must never cause a false-positive Set call.

    When mode is LoadBalance, reservePercent is always 0 and serverRole is always
    Active on both sides.  Comparing them would always be 0==0 / Active==Active,
    which is harmless — but this test proves no cmdlet fires at all.
    """
    current = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))

    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    assert ps_commands == [], (
        "No PowerShell calls expected — identical LoadBalance config with normalized fields"
    )


# ---------------------------------------------------------------------------
# Options-merge: DNS + gateway changes produce a single Set-DhcpServerv4OptionValue
# ---------------------------------------------------------------------------

async def test_dns_and_gateway_changed_single_options_call():
    """When both DNS and gateway change, only ONE Set-DhcpServerv4OptionValue is issued.

    Before the options-merge fix there were two calls: one for DNS/domain, one for gateway.
    The merged path issues a single combined call that sets DNS, domain, and -Router together,
    avoiding a redundant PowerShell process per reconciliation cycle.
    """
    current = _make_scope(dnsServers=["10.0.0.53"], gateway="10.20.30.1")
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"], gateway="10.20.30.2")
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    set_options_calls = [cmd for cmd in ps_commands if "Set-DhcpServerv4OptionValue" in cmd]
    assert len(set_options_calls) == 1, (
        f"Expected exactly 1 Set-DhcpServerv4OptionValue call, got {len(set_options_calls)}: "
        f"{set_options_calls}"
    )
    # The single call must carry both DNS and router
    cmd = set_options_calls[0]
    assert "-DnsServer" in cmd
    assert "-Router '10.20.30.2'" in cmd


async def test_dns_changed_gateway_removed_issues_remove_option():
    """When DNS changes and gateway is removed, Set-DhcpServerv4OptionValue runs once
    and Remove-DhcpServerv4OptionValue -OptionId 3 runs once — no duplicate Set calls.
    """
    current = _make_scope(dnsServers=["10.0.0.53"], gateway="10.20.30.1")
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"], gateway=None)
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    set_options_calls = [cmd for cmd in ps_commands if "Set-DhcpServerv4OptionValue" in cmd]
    remove_option_calls = [
        cmd for cmd in ps_commands
        if "Remove-DhcpServerv4OptionValue" in cmd and "-OptionId 3" in cmd
    ]
    assert len(set_options_calls) == 1, (
        f"Expected 1 Set-DhcpServerv4OptionValue, got {len(set_options_calls)}"
    )
    assert len(remove_option_calls) == 1, (
        f"Expected 1 Remove-DhcpServerv4OptionValue -OptionId 3, got {len(remove_option_calls)}"
    )
    # Set call must NOT include -Router when gateway is None
    assert "-Router" not in set_options_calls[0]


async def test_only_gateway_changed_no_dns_single_options_call():
    """When only gateway changes (DNS unchanged), still only one Set-DhcpServerv4OptionValue."""
    current = _make_scope(gateway="10.20.30.1")
    desired = _make_scope(gateway="10.20.30.2")
    calls = await _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    set_options_calls = [cmd for cmd in ps_commands if "Set-DhcpServerv4OptionValue" in cmd]
    assert len(set_options_calls) == 1
    assert "-Router '10.20.30.2'" in set_options_calls[0]


# ---------------------------------------------------------------------------
# PXE boot options (DHCP 66/67)
# ---------------------------------------------------------------------------

_PXE = dict(nextServer="boot.lab.local", bootFile="snponly.efi")


async def test_boot_options_added():
    """Adding a PXE pair writes options 66 and 67, one Set call each."""
    current = _make_scope()
    desired = _make_scope(**_PXE)
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    assert any(
        "-OptionId 66" in cmd and "-Value 'boot.lab.local'" in cmd for cmd in ps_commands
    )
    assert any(
        "-OptionId 67" in cmd and "-Value 'snponly.efi'" in cmd for cmd in ps_commands
    )


async def test_boot_options_removed():
    """Clearing the pair removes both options — Windows keeps a value that stops being written."""
    current = _make_scope(**_PXE)
    desired = _make_scope()
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    for option_id in (66, 67):
        assert any(
            "Remove-DhcpServerv4OptionValue" in cmd and f"-OptionId {option_id}" in cmd
            for cmd in ps_commands
        ), f"expected option {option_id} to be removed"
    assert not any("-OptionId 66 -Value" in cmd for cmd in ps_commands)


async def test_boot_file_changed_alone_rewrites_both():
    """Repointing only the boot file still rewrites the pair — two Set calls, no removals."""
    current = _make_scope(**_PXE)
    desired = _make_scope(nextServer="boot.lab.local", bootFile="ipxe.efi")
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    assert any("-OptionId 67" in cmd and "-Value 'ipxe.efi'" in cmd for cmd in ps_commands)
    assert not any("Remove-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


async def test_boot_options_unchanged_issues_no_calls():
    """An unchanged PXE pair must not re-issue option writes — that would be a PUT loop."""
    scope = _make_scope(**_PXE)
    assert await _run_update(scope, scope) == []


async def test_boot_only_change_does_not_rewrite_dns():
    """A PXE-only change must not re-issue the DNS/router write, and vice versa.

    This is why boot options are diffed in their own block rather than folded into
    options_changed — a shared flag would make each change rewrite the other's options.
    """
    current = _make_scope()
    desired = _make_scope(**_PXE)
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    assert not any("-DnsServer" in cmd for cmd in ps_commands)


async def test_dns_only_change_does_not_touch_boot_options():
    current = _make_scope(dnsServers=["10.0.0.53"], **_PXE)
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"], **_PXE)
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    assert any("-DnsServer" in cmd for cmd in ps_commands)
    assert not any("-OptionId 66" in cmd or "-OptionId 67" in cmd for cmd in ps_commands)


# ---------------------------------------------------------------------------
# Range transitions
#
# Set-DhcpServerv4Scope only accepts a new range that is a superset or a subset
# of the current one, so mixed and disjoint moves are routed through the union.
# ---------------------------------------------------------------------------

def _ip(text):
    from ipaddress import IPv4Address
    return IPv4Address(text)


def _steps(current, desired):
    from app.services.scope_service import _range_transition_steps
    return _range_transition_steps(
        (_ip(current[0]), _ip(current[1])),
        (_ip(desired[0]), _ip(desired[1])),
    )


async def test_range_steps_no_change():
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.50", "10.20.30.200")) == []


async def test_range_steps_pure_widening_is_one_write():
    """Desired is already a superset — Windows takes it directly."""
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.40", "10.20.30.250")) == [
        (_ip("10.20.30.40"), _ip("10.20.30.250"))
    ]


async def test_range_steps_pure_narrowing_is_one_write():
    """Desired is already a subset — Windows takes it directly."""
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.60", "10.20.30.190")) == [
        (_ip("10.20.30.60"), _ip("10.20.30.190"))
    ]


async def test_range_steps_single_edge_moves_are_one_write():
    assert len(_steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.40", "10.20.30.200"))) == 1
    assert len(_steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.50", "10.20.30.180"))) == 1
    assert len(_steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.60", "10.20.30.200"))) == 1
    assert len(_steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.50", "10.20.30.250"))) == 1


async def test_range_steps_mixed_start_out_end_in_goes_via_union():
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.40", "10.20.30.180")) == [
        (_ip("10.20.30.40"), _ip("10.20.30.200")),   # union: superset of current
        (_ip("10.20.30.40"), _ip("10.20.30.180")),   # desired: subset of union
    ]


async def test_range_steps_mixed_start_in_end_out_goes_via_union():
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.60", "10.20.30.250")) == [
        (_ip("10.20.30.50"), _ip("10.20.30.250")),
        (_ip("10.20.30.60"), _ip("10.20.30.250")),
    ]


async def test_range_steps_disjoint_goes_via_union():
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.10", "10.20.30.40")) == [
        (_ip("10.20.30.10"), _ip("10.20.30.200")),
        (_ip("10.20.30.10"), _ip("10.20.30.40")),
    ]
    assert _steps(("10.20.30.50", "10.20.30.200"), ("10.20.30.210", "10.20.30.250")) == [
        (_ip("10.20.30.50"), _ip("10.20.30.250")),
        (_ip("10.20.30.210"), _ip("10.20.30.250")),
    ]


async def test_range_steps_every_step_is_a_superset_or_subset_of_its_predecessor():
    """The property the whole helper exists to guarantee."""
    edges = ["10.20.30.10", "10.20.30.50", "10.20.30.120", "10.20.30.200", "10.20.30.250"]
    pairs = [(a, b) for a in edges for b in edges if _ip(a) < _ip(b)]
    for current in pairs:
        for desired in pairs:
            prev = (_ip(current[0]), _ip(current[1]))
            for step in _steps(current, desired):
                superset = step[0] <= prev[0] and step[1] >= prev[1]
                subset = step[0] >= prev[0] and step[1] <= prev[1]
                assert superset or subset, (
                    f"{current} -> {desired}: step {step} is neither a superset "
                    f"nor a subset of {prev}, Windows would refuse it"
                )
                prev = step
            assert prev == (_ip(desired[0]), _ip(desired[1]))


async def test_range_change_is_separate_from_scope_params():
    """A combined write applies name/lease/description even when the range is refused."""
    current = _make_scope(startRange="10.20.30.100", endRange="10.20.30.200")
    desired = _make_scope(
        scopeName="New Name", startRange="10.20.30.90", endRange="10.20.30.190"
    )
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    range_writes = [c for c in ps_commands if "-StartRange" in c]
    param_writes = [c for c in ps_commands if "-Name " in c]
    assert range_writes, "expected a range write"
    assert param_writes, "expected a scope-parameter write"
    assert not any("-Name " in c for c in range_writes), "range write must not carry -Name"
    assert not any("-StartRange" in c for c in param_writes), "param write must not carry the range"
    assert ps_commands.index(range_writes[0]) < ps_commands.index(param_writes[0]), (
        "the range must be written first so a refused range aborts before "
        "name/lease/description have been applied"
    )


async def test_mixed_range_change_issues_two_range_writes_union_first():
    current = _make_scope(startRange="10.20.30.100", endRange="10.20.30.200")
    desired = _make_scope(startRange="10.20.30.90", endRange="10.20.30.190")
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    range_writes = [c for c in ps_commands if "-StartRange" in c]
    assert len(range_writes) == 2, f"expected union + desired, got {range_writes}"
    assert "'10.20.30.90'" in range_writes[0] and "'10.20.30.200'" in range_writes[0]
    assert "'10.20.30.90'" in range_writes[1] and "'10.20.30.190'" in range_writes[1]


async def test_name_only_change_issues_no_range_write():
    current = _make_scope(scopeName="Old Name")
    desired = _make_scope(scopeName="New Name")
    ps_commands = [c.args[0] for c in await _run_update(current, desired)]
    assert not any("-StartRange" in cmd for cmd in ps_commands)


async def test_subnet_mask_change_is_rejected_before_any_write():
    from app.errors import ImmutableScopeFieldError
    current = _make_scope(subnetMask="255.255.255.0")
    # A /25 payload that is valid in its own right — the rejection must come from
    # the mask differing from observed state, not from body validation.
    desired = _make_scope(
        subnetMask="255.255.255.128",
        startRange="10.20.30.100",
        endRange="10.20.30.120",
        gateway="10.20.30.126",
    )
    with pytest.raises(ImmutableScopeFieldError) as exc:
        await _run_update(current, desired)
    assert exc.value.field == "subnetMask"
    assert exc.value.status_code == 409
