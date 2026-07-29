from __future__ import annotations
import logging
from typing import Optional

from app.errors import ScopeNotFoundError
from app.models import DhcpFailover, DhcpScopeListError, DhcpScopeListResponse, DhcpScopePayload
from app.errors import PowerShellError
from app.services.ps_executor import is_already_exists_error, is_not_found_error, run_ps
from app.services.ps_parsers import (
    _extract_ip_str,
    assemble_scope_state,
    build_get_all_scopes_script,
    build_payload_from_scope_state,
    normalize_get_scope_state,
    normalize_list,
)
from app.utils.decorators import log_call
from app.utils.ip_utils import ip_to_int
from app.utils.locks import scope_locks
from app.utils.powershell import ps_ipv4, ps_ipv4_csv, ps_single_quote

logger = logging.getLogger(__name__)


def _scope_extra(scope: str, operation: str, **extra: object) -> dict[str, object]:
    return {"scope": str(scope), "operation": operation, **extra}


def _set_options_command(scope_literal: str, payload: DhcpScopePayload) -> str:
    # -Force is required, not cosmetic. Set-DhcpServerv4OptionValue validates
    # that each -DnsServer is a resolvable DNS server and rejects the whole call
    # otherwise ("10.10.1.5 is not a valid DNS server"), which aborts scope
    # creation before exclusions are ever applied.
    #
    # Desired state comes from Git and is authoritative, so the API must apply
    # it rather than second-guess reachability from the DHCP server. Without
    # -Force a DNS server that is merely unreachable at that moment — or simply
    # not resolvable from this host — permanently blocks reconciliation, and
    # Crossplane retries the same failing POST forever.
    cmd = (
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_literal} "
        f"-DnsServer {ps_ipv4_csv(payload.dnsServers)} "
        f"-DnsDomain {ps_single_quote(payload.dnsDomain)}"
    )
    if payload.gateway is not None:
        cmd += f" -Router {ps_ipv4(payload.gateway)}"
    return cmd + " -Force"


def _set_boot_options_commands(scope_literal: str, payload: DhcpScopePayload) -> list[str]:
    """Commands that write DHCP options 66/67, or [] when the scope has no PXE config.

    These cannot join _set_options_command: Set-DhcpServerv4OptionValue exposes named
    parameters only for options 3/6/15 (-Router/-DnsServer/-DnsDomain), and -OptionId
    takes a single id per invocation. So the PXE pair costs one call per option.

    Checking nextServer alone is sufficient — DhcpScopeBody.pxe_options_set_together
    guarantees the two are both set or both empty.
    """
    if not payload.nextServer:
        return []
    return [
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_literal} "
        f"-OptionId 66 -Value {ps_single_quote(payload.nextServer)} -Force",
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_literal} "
        f"-OptionId 67 -Value {ps_single_quote(payload.bootFile)} -Force",
    ]


def _remove_boot_options_commands(scope_literal: str) -> list[str]:
    """Commands that clear DHCP options 66/67.

    Windows does not drop an option value just because it stopped being written, so
    clearing the pair needs explicit removals — same reason the router option has an
    explicit Remove in update_scope. One call per id, mirroring the -OptionId 3 removal.
    """
    return [
        f"Remove-DhcpServerv4OptionValue -ScopeId {scope_literal} -OptionId {option_id}"
        for option_id in (66, 67)
    ]


# ---------------------------------------------------------------------------
# PS execution helper  (single entry point with explicit error-handling policy)
# ---------------------------------------------------------------------------

async def _run_ps(
    cmd: str,
    *,
    parse_json: bool = False,
    ignore_not_found: bool = False,
    ignore_already_exists: bool = False,
    warn_prefix: str | None = None,
    scope: str | None = None,
    operation: str | None = None,
    relationship_name: str | None = None,
) -> dict | list | None:
    """Central PowerShell execution helper with explicit error-handling policy.

    Policy flags (set exactly one per call to keep intent unambiguous):
        ignore_not_found      – return None on not-found errors; re-raise all others.
                                Use for: optional removals where absence is expected.
        ignore_already_exists – silently accept "already exists/added/in use" errors.
                                Use for: idempotent create operations.
        warn_prefix           – log a warning with this prefix instead of raising.
                                Use for: per-item cleanup where partial failure is tolerable.
    """
    try:
        return await run_ps(
            cmd,
            parse_json=parse_json,
            scope=scope,
            operation=operation,
            relationship_name=relationship_name,
        )
    except PowerShellError as exc:
        if warn_prefix is not None:
            logger.warning(
                "%s: %s",
                warn_prefix,
                exc.safe_stderr_preview,
                extra=_scope_extra(scope or "", operation or "powershell", status="ignored"),
            )
            return None
        if ignore_not_found and is_not_found_error(exc.stderr):
            logger.info(
                "Ignoring DHCP object not found",
                extra=_scope_extra(scope or "", operation or "powershell", status="ignored"),
            )
            return None
        if ignore_already_exists and is_already_exists_error(exc.stderr):
            logger.info(
                "Ignoring DHCP object already exists",
                extra=_scope_extra(scope or "", operation or "powershell", status="ignored"),
            )
            return None
        raise


async def _try_assemble_scope(scope: str) -> Optional[DhcpScopePayload]:
    """Assemble scope state for delete pre-flight; return None if scope is not found.

    Only suppresses not-found errors (scope disappeared between existence check and
    assembly — a benign race condition). All other PowerShellErrors are re-raised so
    that unexpected failures (permission denied, PS crash, etc.) surface as 500 and
    Crossplane retries the DELETE, rather than receiving a false 204 that causes it
    to remove the CR while the scope remains on the DHCP server.
    """
    try:
        return await assemble_scope_state(scope)
    except PowerShellError as exc:
        if is_not_found_error(exc.stderr):
            return None
        raise


async def _assemble_existing_scope(scope: str) -> DhcpScopePayload:
    """Assemble a scope and translate legitimate missing-scope errors to domain 404."""
    try:
        return await assemble_scope_state(scope)
    except PowerShellError as exc:
        if is_not_found_error(exc.stderr):
            raise ScopeNotFoundError(str(scope)) from exc
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@log_call
async def list_scopes() -> DhcpScopeListResponse:
    """Return all DHCP scopes sorted by network address (ascending).

    Uses a single PowerShell process that fetches scope + options + exclusions +
    failover for every scope in one pass, instead of one process per scope.
    This reduces overhead from O(N) processes to O(1) for a fleet of N scopes.

    If the DHCP server has no scopes, PowerShell returns null and empty lists
    are returned.  Any PowerShell error (connection failure, permission denied,
    etc.) raises and results in a 500 — the entire list is unavailable in that
    case.  Per-scope assembly errors (Pydantic validation, missing DNS option,
    unexpected data format) are caught individually: the broken scope is added
    to `errors` and all other scopes are still returned.
    """
    logger.info("Listing DHCP scopes", extra={"operation": "list_scopes"})
    script = build_get_all_scopes_script()
    # Call run_ps directly (not _run_ps) so any PowerShell failure propagates as
    # an exception — a global PS error means no scopes are available at all.
    raw = await run_ps(
        script,
        append_error_action=False,
        append_convert_to_json=False,
        operation="list_scopes",
    )
    # normalize_list handles: None (no scopes) → [], dict (one scope) → [dict], list → list
    entries = normalize_list(raw)
    payloads: list[DhcpScopePayload] = []
    errors: list[DhcpScopeListError] = []
    for entry in entries:
        state = normalize_get_scope_state(entry)
        scope = _extract_ip_str(state["scope"].get("ScopeId") or "").strip()
        if not scope:
            continue
        try:
            payloads.append(build_payload_from_scope_state(scope, state))
        except Exception as exc:
            logger.warning(
                "Skipping scope with invalid data during list",
                extra=_scope_extra(scope, "list_scopes", status="error"),
                exc_info=True,
            )
            errors.append(DhcpScopeListError(scope=scope, error=str(exc)))
    return DhcpScopeListResponse(
        scopes=sorted(payloads, key=lambda p: ip_to_int(p.scope)),
        errors=errors,
    )


@log_call
async def scope_exists(scope: str) -> bool:
    scope_literal = ps_ipv4(scope)
    script = (
        f"$found = $false\n"
        f"foreach ($s in @(Get-DhcpServerv4Scope -ErrorAction Stop)) {{\n"
        f"    if ($s.ScopeId.ToString() -eq {scope_literal}) {{ $found = $true; break }}\n"
        f"}}\n"
        f"$found | ConvertTo-Json -Compress"
    )
    result = await run_ps(
        script,
        parse_json=True,
        append_error_action=False,
        append_convert_to_json=False,
        scope=scope,
        operation="scope_exists",
    )
    return result is True


@log_call
async def create_scope(payload: DhcpScopePayload) -> DhcpScopePayload:
    scope = str(payload.scope)
    scope_literal = ps_ipv4(scope)
    logger.info("Creating DHCP scope", extra=_scope_extra(scope, "create_scope"))

    async with scope_locks.lock(scope):
        already_existed = await scope_exists(scope)
        if not already_existed:
            await _run_ps(
                f'Add-DhcpServerv4Scope '
                f'-Name {ps_single_quote(payload.scopeName)} '
                f'-StartRange {ps_ipv4(payload.startRange)} '
                f'-EndRange {ps_ipv4(payload.endRange)} '
                f'-SubnetMask {ps_ipv4(payload.subnetMask)} '
                f'-State Active '
                f'-LeaseDuration (New-TimeSpan -Days {payload.leaseDurationDays}) '
                f'-Description {ps_single_quote(payload.description)}',
                ignore_already_exists=True,
                scope=scope,
                operation="add_scope",
            )
        else:
            logger.info(
                "Scope already exists, converging desired state",
                extra=_scope_extra(scope, "create_scope", status="already_exists"),
            )

        await run_ps(
            _set_options_command(scope_literal, payload),
            parse_json=False,
            scope=scope,
            operation="set_dns_options",
        )

        # PXE options 66/67. A scope with no PXE config costs zero extra calls here,
        # which is the common case.
        for cmd in _set_boot_options_commands(scope_literal, payload):
            await run_ps(
                cmd,
                parse_json=False,
                scope=scope,
                operation="set_boot_options",
            )
        # POST must converge, not just create (§2), so a pre-existing scope carrying
        # stale boot options has them cleared when the desired state has none. Skipped
        # for a freshly added scope, which cannot have any.
        if already_existed and not payload.nextServer:
            for cmd in _remove_boot_options_commands(scope_literal):
                await _run_ps(
                    cmd,
                    parse_json=False,
                    ignore_not_found=True,
                    scope=scope,
                    operation="remove_boot_options",
                )

        for excl in payload.exclusions:
            await _run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(excl.startAddress)} -EndRange {ps_ipv4(excl.endAddress)}",
                ignore_already_exists=True,
                scope=scope,
                operation="add_exclusion",
            )

        if payload.failover is not None:
            await _setup_failover(scope, payload.failover)
            await _replicate_failover(scope, payload.failover.relationshipName)

        return await assemble_scope_state(scope)


@log_call
async def get_scope(scope: str) -> DhcpScopePayload:
    logger.info("Getting DHCP scope", extra=_scope_extra(scope, "get_scope"))
    return await _assemble_existing_scope(scope)


@log_call
async def update_scope(scope: str, desired: DhcpScopePayload) -> DhcpScopePayload:
    scope_literal = ps_ipv4(scope)
    logger.info("Updating DHCP scope", extra=_scope_extra(scope, "update_scope"))
    async with scope_locks.lock(scope):
        current = await _assemble_existing_scope(scope)
        changed = False

        if (
            current.scopeName != desired.scopeName
            or current.leaseDurationDays != desired.leaseDurationDays
            or current.description != desired.description
            or current.startRange != desired.startRange
            or current.endRange != desired.endRange
        ):
            changed = True
            logger.info(
                "Updating DHCP scope parameters",
                extra=_scope_extra(scope, "set_scope_params"),
            )
            await run_ps(
                f"Set-DhcpServerv4Scope -ScopeId {scope_literal} "
                f"-Name {ps_single_quote(desired.scopeName)} "
                f"-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) "
                f"-Description {ps_single_quote(desired.description)} "
                f"-StartRange {ps_ipv4(desired.startRange)} "
                f"-EndRange {ps_ipv4(desired.endRange)}",
                parse_json=False,
                scope=scope,
                operation="set_scope_params",
            )

        options_changed = (
            current.dnsServers != desired.dnsServers
            or current.dnsDomain != desired.dnsDomain
            or current.gateway != desired.gateway
        )
        if options_changed:
            changed = True
            logger.info(
                "Updating DHCP scope options",
                extra=_scope_extra(scope, "set_dns_options"),
            )
            # Single combined call: sets DNS servers, domain, and gateway (when not None).
            # Merging DNS and gateway into one cmdlet call avoids a redundant PowerShell
            # process when both change simultaneously.
            await run_ps(
                _set_options_command(scope_literal, desired),
                parse_json=False,
                scope=scope,
                operation="set_dns_options",
            )
            # If gateway is being removed (transitioned to None), explicitly remove
            # DHCP option 3. _set_options_command omits -Router when gateway is None,
            # but Windows DHCP does not clear an existing router option automatically.
            if current.gateway != desired.gateway and desired.gateway is None:
                logger.info(
                    "Removing DHCP router option",
                    extra=_scope_extra(scope, "remove_router_option"),
                )
                await _run_ps(
                    f"Remove-DhcpServerv4OptionValue -ScopeId {scope_literal} "
                    f"-OptionId 3",
                    parse_json=False,
                    ignore_not_found=True,
                    scope=scope,
                    operation="remove_router_option",
                )

        # Boot options are diffed separately from the DNS/router block above: they need
        # their own cmdlet calls either way, and keeping them apart means a PXE-only
        # change does not re-issue the DNS write (and vice versa).
        boot_options_changed = (
            current.nextServer != desired.nextServer
            or current.bootFile != desired.bootFile
        )
        if boot_options_changed:
            changed = True
            if desired.nextServer:
                logger.info(
                    "Updating DHCP boot options",
                    extra=_scope_extra(scope, "set_boot_options"),
                )
                for cmd in _set_boot_options_commands(scope_literal, desired):
                    await run_ps(
                        cmd,
                        parse_json=False,
                        scope=scope,
                        operation="set_boot_options",
                    )
            else:
                logger.info(
                    "Removing DHCP boot options",
                    extra=_scope_extra(scope, "remove_boot_options"),
                )
                for cmd in _remove_boot_options_commands(scope_literal):
                    await _run_ps(
                        cmd,
                        parse_json=False,
                        ignore_not_found=True,
                        scope=scope,
                        operation="remove_boot_options",
                    )

        current_excl = {(e.startAddress, e.endAddress) for e in current.exclusions}
        desired_excl = {(e.startAddress, e.endAddress) for e in desired.exclusions}

        for start, end in current_excl - desired_excl:
            changed = True
            logger.info(
                "Removing DHCP exclusion range",
                extra=_scope_extra(scope, "remove_exclusion"),
            )
            await run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(start)} -EndRange {ps_ipv4(end)}",
                parse_json=False,
                scope=scope,
                operation="remove_exclusion",
            )

        for start, end in desired_excl - current_excl:
            changed = True
            logger.info(
                "Adding DHCP exclusion range",
                extra=_scope_extra(scope, "add_exclusion"),
            )
            await run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(start)} -EndRange {ps_ipv4(end)}",
                parse_json=False,
                scope=scope,
                operation="add_exclusion",
            )

        failover_changed = await _handle_failover_diff(scope, current.failover, desired.failover)
        changed = changed or failover_changed

        if changed and desired.failover is not None:
            await _replicate_failover(scope, desired.failover.relationshipName)

        return await _assemble_existing_scope(scope)


@log_call
async def delete_scope(scope: str) -> None:
    scope_literal = ps_ipv4(scope)
    logger.info("Received delete request for DHCP scope", extra=_scope_extra(scope, "delete_scope"))
    async with scope_locks.lock(scope):
        if not await scope_exists(scope):
            logger.info(
                "Scope not found — already absent, returning 204 (idempotent delete)",
                extra=_scope_extra(scope, "delete_scope", status="not_found"),
            )
            return

        current = await _try_assemble_scope(scope)
        if current is None:
            logger.info(
                "Scope disappeared between existence check and state assembly — "
                "likely a concurrent delete, returning 204",
                extra=_scope_extra(scope, "delete_scope", status="not_found"),
            )
            return

        if current.failover is not None:
            await _remove_scope_from_failover(scope, current.failover.relationshipName)

        for excl in current.exclusions:
            await _run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(excl.startAddress)} -EndRange {ps_ipv4(excl.endAddress)}",
                warn_prefix=f"Failed to remove exclusion {excl.startAddress}",
                scope=scope,
                operation="remove_exclusion",
            )

        await run_ps(
            f"Remove-DhcpServerv4Scope -ScopeId {scope_literal} -Force",
            parse_json=False,
            scope=scope,
            operation="remove_scope",
        )
        logger.info("DHCP scope deleted", extra=_scope_extra(scope, "delete_scope", status="ok"))


# ---------------------------------------------------------------------------
# Failover helpers
# ---------------------------------------------------------------------------

async def _fetch_failover_by_name(
    rel_name: str, scope: str | None = None
) -> dict | None:
    """Return the failover relationship object for rel_name, or None if it doesn't exist.

    Uses an aggregate Get-DhcpServerv4Failover (no -Name flag) so that a missing
    relationship is a normal empty result rather than a terminating PS error.
    Real errors (server unreachable, permission denied) still propagate.
    """
    rel_literal = ps_single_quote(rel_name)
    script = (
        f"$fo = $null\n"
        f"foreach ($f in @(Get-DhcpServerv4Failover -ErrorAction Stop)) {{\n"
        f"    if ($f.Name -eq {rel_literal}) {{ $fo = $f; break }}\n"
        f"}}\n"
        f"$fo | ConvertTo-Json -Depth 10 -Compress"
    )
    raw = await run_ps(
        script,
        parse_json=True,
        append_error_action=False,
        append_convert_to_json=False,
        scope=scope,
        operation="get_failover",
        relationship_name=rel_name,
    )
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


@log_call
async def _replicate_failover(scope: str, relationship_name: str | None = None) -> None:
    await run_ps(
        f"Invoke-DhcpServerv4FailoverReplication -ScopeId {ps_ipv4(scope)} -Force",
        parse_json=False,
        scope=scope,
        operation="replicate_failover",
        relationship_name=relationship_name,
    )
    logger.info(
        "Failover replication completed",
        extra=_scope_extra(
            scope,
            "replicate_failover",
            relationship_name=relationship_name,
            status="ok",
        ),
    )


@log_call
async def _remove_scope_from_failover(scope: str, rel_name: str) -> None:
    await run_ps(
        f"Remove-DhcpServerv4FailoverScope -Name {ps_single_quote(rel_name)} "
        f"-ScopeId {ps_ipv4(scope)} -Force",
        parse_json=False,
        scope=scope,
        operation="remove_failover_scope",
        relationship_name=rel_name,
    )
    rel_raw = await _fetch_failover_by_name(rel_name, scope=scope)
    if isinstance(rel_raw, dict) and not rel_raw.get("ScopeId"):
            await run_ps(
                f"Remove-DhcpServerv4Failover -Name {ps_single_quote(rel_name)} -Force",
                parse_json=False,
                scope=scope,
                operation="remove_failover_relationship",
                relationship_name=rel_name,
            )


@log_call
async def _setup_failover(scope: str, failover: DhcpFailover) -> None:
    existing = await _fetch_failover_by_name(failover.relationshipName, scope=scope)
    if existing:
        await _run_ps(
            f"Add-DhcpServerv4FailoverScope -Name {ps_single_quote(failover.relationshipName)} "
            f"-ScopeId {ps_ipv4(scope)}",
            ignore_already_exists=True,
            scope=scope,
            operation="add_failover_scope",
            relationship_name=failover.relationshipName,
        )
    else:
        await _create_failover_relationship(scope, failover)


@log_call
async def _create_failover_relationship(scope: str, failover: DhcpFailover) -> None:
    # Add-DhcpServerv4Failover has no -Mode parameter (unlike Set-DhcpServerv4Failover,
    # which does). It resolves mode from the parameter set instead: -ServerRole /
    # -ReservePercent select HotStandby, -LoadBalancePercent selects LoadBalance.
    # Passing -Mode fails binding with NamedParameterNotFound before the cmdlet runs.
    cmd = (
        f'Add-DhcpServerv4Failover '
        f'-Name {ps_single_quote(failover.relationshipName)} '
        f'-PartnerServer {ps_single_quote(failover.partnerServer)} '
        f'-ScopeId {ps_ipv4(scope)} '
        f'-MaxClientLeadTime (New-TimeSpan -Minutes {failover.maxClientLeadTimeMinutes}) '
        f'-Force'
    )
    if failover.mode == "HotStandby":
        cmd += f" -ServerRole {failover.serverRole}"
        cmd += f" -ReservePercent {failover.reservePercent}"
    else:
        cmd += f" -LoadBalancePercent {failover.loadBalancePercent}"

    await run_ps(
        cmd,
        parse_json=False,
        scope=scope,
        operation="create_failover_relationship",
        relationship_name=failover.relationshipName,
    )


@log_call
async def _handle_failover_diff(
    scope: str,
    current: Optional[DhcpFailover],
    desired: Optional[DhcpFailover],
) -> bool:
    if current is None and desired is None:
        return False

    if current is None:
        assert desired is not None
        await _setup_failover(scope, desired)
        return True

    if desired is None:
        await _remove_scope_from_failover(scope, current.relationshipName)
        return True

    if current.mode != desired.mode:
        logger.info(
            "Failover mode changed, recreating relationship",
            extra=_scope_extra(
                scope,
                "recreate_failover",
                relationship_name=current.relationshipName,
            ),
        )
        await _remove_scope_from_failover(scope, current.relationshipName)
        await _setup_failover(scope, desired)
        return True

    identity_changed = (
        current.relationshipName != desired.relationshipName
        or current.partnerServer != desired.partnerServer
        or (current.mode == "HotStandby" and current.serverRole != desired.serverRole)
    )
    if identity_changed:
        logger.info(
            "Failover identity changed, recreating relationship",
            extra=_scope_extra(
                scope,
                "recreate_failover",
                relationship_name=current.relationshipName,
            ),
        )
        await _remove_scope_from_failover(scope, current.relationshipName)
        await _setup_failover(scope, desired)
        return True

    if current.mode == "HotStandby":
        mutable_changed = (
            current.reservePercent != desired.reservePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
        )
    else:
        mutable_changed = (
            current.loadBalancePercent != desired.loadBalancePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
        )

    if mutable_changed:
        logger.info(
            "Updating failover parameters",
            extra=_scope_extra(
                scope,
                "set_failover_params",
                relationship_name=current.relationshipName,
            ),
        )
        cmd = (
            f"Set-DhcpServerv4Failover -Name {ps_single_quote(current.relationshipName)} "
            f"-MaxClientLeadTime (New-TimeSpan -Minutes {desired.maxClientLeadTimeMinutes})"
        )
        if desired.mode == "HotStandby":
            cmd += f" -ReservePercent {desired.reservePercent}"
        else:
            cmd += f" -LoadBalancePercent {desired.loadBalancePercent}"
        await run_ps(
            cmd,
            parse_json=False,
            scope=scope,
            operation="set_failover_params",
            relationship_name=current.relationshipName,
        )
        return True

    return False
