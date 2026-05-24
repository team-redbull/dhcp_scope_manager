from __future__ import annotations
import logging
from typing import Optional

from app.errors import ScopeNotFoundError
from app.models import DhcpFailover, DhcpScopeListError, DhcpScopeListResponse, DhcpScopePayload
from app.errors import PowerShellError
from app.services.ps_executor import is_already_exists_error, is_not_found_error, run_ps
from app.services.ps_parsers import (
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


def _scope_extra(scope_id: str, operation: str, **extra: object) -> dict[str, object]:
    return {"scope_id": str(scope_id), "operation": operation, **extra}


def _set_options_command(scope_literal: str, payload: DhcpScopePayload) -> str:
    cmd = (
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_literal} "
        f"-DnsServer {ps_ipv4_csv(payload.dnsServers)} "
        f"-DnsDomain {ps_single_quote(payload.dnsDomain)}"
    )
    if payload.gateway is not None:
        cmd += f" -Router {ps_ipv4(payload.gateway)}"
    return cmd


# ---------------------------------------------------------------------------
# PS execution helper  (single entry point with explicit error-handling policy)
# ---------------------------------------------------------------------------

async def _run_ps(
    cmd: str,
    *,
    parse_json: bool = False,
    ignore_not_found: bool = False,
    ignore_already_exists: bool = False,
    best_effort: bool = False,
    warn_prefix: str | None = None,
    scope_id: str | None = None,
    operation: str | None = None,
    relationship_name: str | None = None,
) -> dict | list | None:
    """Central PowerShell execution helper with explicit error-handling policy.

    Policy flags (set exactly one per call to keep intent unambiguous):
        ignore_not_found      – return None on not-found errors; re-raise all others.
                                Use for: existence checks, optional lookups.
        ignore_already_exists – silently accept "already exists/added/in use" errors.
                                Use for: idempotent create operations.
        best_effort           – return None on any PowerShellError.
                                Use for: best-effort cleanup where any failure is OK.
        warn_prefix           – log a warning with this prefix instead of raising.
                                Use for: per-item cleanup where partial failure is tolerable.
    """
    try:
        return await run_ps(
            cmd,
            parse_json=parse_json,
            scope_id=scope_id,
            operation=operation,
            relationship_name=relationship_name,
        )
    except PowerShellError as exc:
        if best_effort:
            logger.warning(
                "Ignoring best-effort PowerShell failure",
                extra=_scope_extra(scope_id or "", operation or "powershell", status="ignored"),
            )
            return None
        if warn_prefix is not None:
            logger.warning(
                "%s: %s",
                warn_prefix,
                exc.safe_stderr_preview,
                extra=_scope_extra(scope_id or "", operation or "powershell", status="ignored"),
            )
            return None
        if ignore_not_found and is_not_found_error(exc.stderr):
            logger.info(
                "Ignoring DHCP object not found",
                extra=_scope_extra(scope_id or "", operation or "powershell", status="ignored"),
            )
            return None
        if ignore_already_exists and is_already_exists_error(exc.stderr):
            logger.info(
                "Ignoring DHCP object already exists",
                extra=_scope_extra(scope_id or "", operation or "powershell", status="ignored"),
            )
            return None
        raise


async def _try_assemble_scope(scope_id: str) -> Optional[DhcpScopePayload]:
    """Assemble scope state for delete pre-flight; return None if scope is not found.

    Only suppresses not-found errors (scope disappeared between existence check and
    assembly — a benign race condition). All other PowerShellErrors are re-raised so
    that unexpected failures (permission denied, PS crash, etc.) surface as 500 and
    Crossplane retries the DELETE, rather than receiving a false 204 that causes it
    to remove the CR while the scope remains on the DHCP server.
    """
    try:
        return await assemble_scope_state(scope_id)
    except PowerShellError as exc:
        if is_not_found_error(exc.stderr):
            return None
        raise


async def _assemble_existing_scope(scope_id: str) -> DhcpScopePayload:
    """Assemble a scope and translate legitimate missing-scope errors to domain 404."""
    try:
        return await assemble_scope_state(scope_id)
    except PowerShellError as exc:
        if is_not_found_error(exc.stderr):
            raise ScopeNotFoundError(str(scope_id)) from exc
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
        scope_id = str(state["scope"].get("ScopeId", "")).strip()
        if not scope_id:
            continue
        try:
            payloads.append(build_payload_from_scope_state(scope_id, state))
        except Exception as exc:
            logger.warning(
                "Skipping scope with invalid data during list",
                extra=_scope_extra(scope_id, "list_scopes", status="error"),
                exc_info=True,
            )
            errors.append(DhcpScopeListError(scopeId=scope_id, error=str(exc)))
    return DhcpScopeListResponse(
        scopes=sorted(payloads, key=lambda p: ip_to_int(p.network)),
        errors=errors,
    )


@log_call
async def scope_exists(scope_id: str) -> bool:
    scope_literal = ps_ipv4(scope_id)
    return await _run_ps(
        f"Get-DhcpServerv4Scope -ScopeId {scope_literal}",
        parse_json=True,
        ignore_not_found=True,
        scope_id=scope_id,
        operation="scope_exists",
    ) is not None


@log_call
async def create_scope(payload: DhcpScopePayload) -> DhcpScopePayload:
    scope_id = str(payload.network)
    scope_literal = ps_ipv4(scope_id)
    logger.info("Creating DHCP scope", extra=_scope_extra(scope_id, "create_scope"))

    async with scope_locks.lock(scope_id):
        if not await scope_exists(scope_id):
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
                scope_id=scope_id,
                operation="add_scope",
            )
        else:
            logger.info(
                "Scope already exists, converging desired state",
                extra=_scope_extra(scope_id, "create_scope", status="already_exists"),
            )

        await run_ps(
            _set_options_command(scope_literal, payload),
            parse_json=False,
            scope_id=scope_id,
            operation="set_dns_options",
        )

        for excl in payload.exclusions:
            await _run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(excl.startAddress)} -EndRange {ps_ipv4(excl.endAddress)}",
                ignore_already_exists=True,
                scope_id=scope_id,
                operation="add_exclusion",
            )

        if payload.failover is not None:
            await _setup_failover(scope_id, payload.failover)
            await _replicate_failover(scope_id, payload.failover.relationshipName)

        return await assemble_scope_state(scope_id)


@log_call
async def get_scope(scope_id: str) -> DhcpScopePayload:
    logger.info("Getting DHCP scope", extra=_scope_extra(scope_id, "get_scope"))
    return await _assemble_existing_scope(scope_id)


@log_call
async def update_scope(scope_id: str, desired: DhcpScopePayload) -> DhcpScopePayload:
    scope_literal = ps_ipv4(scope_id)
    logger.info("Updating DHCP scope", extra=_scope_extra(scope_id, "update_scope"))
    async with scope_locks.lock(scope_id):
        current = await _assemble_existing_scope(scope_id)
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
                extra=_scope_extra(scope_id, "set_scope_params"),
            )
            await run_ps(
                f"Set-DhcpServerv4Scope -ScopeId {scope_literal} "
                f"-Name {ps_single_quote(desired.scopeName)} "
                f"-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) "
                f"-Description {ps_single_quote(desired.description)} "
                f"-StartRange {ps_ipv4(desired.startRange)} "
                f"-EndRange {ps_ipv4(desired.endRange)}",
                parse_json=False,
                scope_id=scope_id,
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
                extra=_scope_extra(scope_id, "set_dns_options"),
            )
            # Single combined call: sets DNS servers, domain, and gateway (when not None).
            # Merging DNS and gateway into one cmdlet call avoids a redundant PowerShell
            # process when both change simultaneously.
            await run_ps(
                _set_options_command(scope_literal, desired),
                parse_json=False,
                scope_id=scope_id,
                operation="set_dns_options",
            )
            # If gateway is being removed (transitioned to None), explicitly remove
            # DHCP option 3. _set_options_command omits -Router when gateway is None,
            # but Windows DHCP does not clear an existing router option automatically.
            if current.gateway != desired.gateway and desired.gateway is None:
                logger.info(
                    "Removing DHCP router option",
                    extra=_scope_extra(scope_id, "remove_router_option"),
                )
                await _run_ps(
                    f"Remove-DhcpServerv4OptionValue -ScopeId {scope_literal} "
                    f"-OptionId 3",
                    parse_json=False,
                    ignore_not_found=True,
                    scope_id=scope_id,
                    operation="remove_router_option",
                )

        current_excl = {(e.startAddress, e.endAddress) for e in current.exclusions}
        desired_excl = {(e.startAddress, e.endAddress) for e in desired.exclusions}

        for start, end in current_excl - desired_excl:
            changed = True
            logger.info(
                "Removing DHCP exclusion range",
                extra=_scope_extra(scope_id, "remove_exclusion"),
            )
            await run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(start)} -EndRange {ps_ipv4(end)}",
                parse_json=False,
                scope_id=scope_id,
                operation="remove_exclusion",
            )

        for start, end in desired_excl - current_excl:
            changed = True
            logger.info(
                "Adding DHCP exclusion range",
                extra=_scope_extra(scope_id, "add_exclusion"),
            )
            await run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(start)} -EndRange {ps_ipv4(end)}",
                parse_json=False,
                scope_id=scope_id,
                operation="add_exclusion",
            )

        failover_changed = await _handle_failover_diff(scope_id, current.failover, desired.failover)
        changed = changed or failover_changed

        if changed and desired.failover is not None:
            await _replicate_failover(scope_id, desired.failover.relationshipName)

        return await _assemble_existing_scope(scope_id)


@log_call
async def delete_scope(scope_id: str) -> None:
    scope_literal = ps_ipv4(scope_id)
    logger.info("Deleting DHCP scope", extra=_scope_extra(scope_id, "delete_scope"))
    async with scope_locks.lock(scope_id):
        if not await scope_exists(scope_id):
            logger.info(
                "DHCP scope does not exist, delete is already converged",
                extra=_scope_extra(scope_id, "delete_scope", status="not_found"),
            )
            return

        current = await _try_assemble_scope(scope_id)
        if current is None:
            return  # scope disappeared between existence check and assembly

        if current.failover is not None:
            await _remove_scope_from_failover(scope_id, current.failover.relationshipName)

        for excl in current.exclusions:
            await _run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_literal} "
                f"-StartRange {ps_ipv4(excl.startAddress)} -EndRange {ps_ipv4(excl.endAddress)}",
                warn_prefix=f"Failed to remove exclusion {excl.startAddress}",
                scope_id=scope_id,
                operation="remove_exclusion",
            )

        await run_ps(
            f"Remove-DhcpServerv4Scope -ScopeId {scope_literal} -Force",
            parse_json=False,
            scope_id=scope_id,
            operation="remove_scope",
        )
        logger.info("DHCP scope deleted", extra=_scope_extra(scope_id, "delete_scope", status="ok"))


# ---------------------------------------------------------------------------
# Failover helpers
# ---------------------------------------------------------------------------

@log_call
async def _replicate_failover(scope_id: str, relationship_name: str | None = None) -> None:
    await run_ps(
        f"Invoke-DhcpServerv4FailoverReplication -ScopeId {ps_ipv4(scope_id)} -Force",
        parse_json=False,
        scope_id=scope_id,
        operation="replicate_failover",
        relationship_name=relationship_name,
    )
    logger.info(
        "Failover replication completed",
        extra=_scope_extra(
            scope_id,
            "replicate_failover",
            relationship_name=relationship_name,
            status="ok",
        ),
    )


@log_call
async def _remove_scope_from_failover(scope_id: str, rel_name: str) -> None:
    await run_ps(
        f"Remove-DhcpServerv4FailoverScope -Name {ps_single_quote(rel_name)} "
        f"-ScopeId {ps_ipv4(scope_id)} -Force",
        parse_json=False,
        scope_id=scope_id,
        operation="remove_failover_scope",
        relationship_name=rel_name,
    )
    rel_raw = await _run_ps(
        f"Get-DhcpServerv4Failover -Name {ps_single_quote(rel_name)}",
        parse_json=True,
        best_effort=True,
        scope_id=scope_id,
        operation="get_failover",
        relationship_name=rel_name,
    )
    if rel_raw:
        rel = rel_raw if isinstance(rel_raw, dict) else rel_raw[0]
        if not rel.get("ScopeId"):
            await run_ps(
                f"Remove-DhcpServerv4Failover -Name {ps_single_quote(rel_name)} -Force",
                parse_json=False,
                scope_id=scope_id,
                operation="remove_failover_relationship",
                relationship_name=rel_name,
            )


@log_call
async def _setup_failover(scope_id: str, failover: DhcpFailover) -> None:
    existing = await _run_ps(
        f"Get-DhcpServerv4Failover -Name {ps_single_quote(failover.relationshipName)}",
        parse_json=True,
        ignore_not_found=True,
        scope_id=scope_id,
        operation="get_failover",
        relationship_name=failover.relationshipName,
    )
    if existing:
        await _run_ps(
            f"Add-DhcpServerv4FailoverScope -Name {ps_single_quote(failover.relationshipName)} "
            f"-ScopeId {ps_ipv4(scope_id)}",
            ignore_already_exists=True,
            scope_id=scope_id,
            operation="add_failover_scope",
            relationship_name=failover.relationshipName,
        )
    else:
        await _create_failover_relationship(scope_id, failover)


@log_call
async def _create_failover_relationship(scope_id: str, failover: DhcpFailover) -> None:
    cmd = (
        f'Add-DhcpServerv4Failover '
        f'-Name {ps_single_quote(failover.relationshipName)} '
        f'-PartnerServer {ps_single_quote(failover.partnerServer)} '
        f'-ScopeId {ps_ipv4(scope_id)} '
        f'-Mode {failover.mode} '
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
        scope_id=scope_id,
        operation="create_failover_relationship",
        relationship_name=failover.relationshipName,
    )


@log_call
async def _handle_failover_diff(
    scope_id: str,
    current: Optional[DhcpFailover],
    desired: Optional[DhcpFailover],
) -> bool:
    if current is None and desired is None:
        return False

    if current is None:
        assert desired is not None
        await _setup_failover(scope_id, desired)
        return True

    if desired is None:
        await _remove_scope_from_failover(scope_id, current.relationshipName)
        return True

    if current.mode != desired.mode:
        logger.info(
            "Failover mode changed, recreating relationship",
            extra=_scope_extra(
                scope_id,
                "recreate_failover",
                relationship_name=current.relationshipName,
            ),
        )
        await _remove_scope_from_failover(scope_id, current.relationshipName)
        await _setup_failover(scope_id, desired)
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
                scope_id,
                "recreate_failover",
                relationship_name=current.relationshipName,
            ),
        )
        await _remove_scope_from_failover(scope_id, current.relationshipName)
        await _setup_failover(scope_id, desired)
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
                scope_id,
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
            scope_id=scope_id,
            operation="set_failover_params",
            relationship_name=current.relationshipName,
        )
        return True

    return False
