import json
import logging
import asyncio
import time

from app.config import settings
from app.errors import PowerShellError, PowerShellExecutionError, PowerShellTimeoutError, sanitize_powershell_text
from app.services import dhcp_service
from app.services.ps_transport import get_transport

logger = logging.getLogger(__name__)


_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_semaphore_limit: int | None = None


def _get_powershell_semaphore() -> asyncio.Semaphore:
    """Return a semaphore bound to the current event loop.

    pytest and ASGI servers may use different event loops over the process
    lifetime, so the semaphore is created lazily for the active loop.
    """
    global _semaphore, _semaphore_loop, _semaphore_limit

    loop = asyncio.get_running_loop()
    limit = settings.POWERSHELL_MAX_CONCURRENCY
    if _semaphore is None or _semaphore_loop is not loop or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_loop = loop
        _semaphore_limit = limit
    return _semaphore


# The DhcpServer module does not describe failures in its messages: a missing
# scope reports "Failed to get scope information for scope 10.20.30.0 on DHCP
# server HOST", which contains no not-found wording at all. Matching on prose
# alone therefore misclassifies every real DHCP error as a hard failure — GET
# returns 500 instead of 404 and Crossplane never learns to POST.
#
# The reliable signals are the PowerShell error category and the DHCP error
# number, both of which appear in stderr (console PowerShell prints them; the
# PSRP transport reproduces them in _format_error_record). They are also
# locale-independent, unlike the message text.
#
# Codes confirmed against Windows Server 2022, DHCP server version 10.0.
_NOT_FOUND_MARKERS = (
    "objectnotfound",   # category: missing scope, unset option, absent failover
    "dhcp 20005",       # scope not found
    "dhcp 20010",       # option value not set on scope
    "dhcp 20116",       # no failover relationship for scope
    # Retained for other cmdlets and older builds that do phrase it plainly.
    "not found",
    "does not exist",
    "no dhcp scope",
    "cannot find",
)

_ALREADY_EXISTS_MARKERS = (
    "resourceexists",   # category: duplicate scope
    "dhcp 20052",       # scope already exists
    # Windows overloads DHCP 20023 across unrelated range failures: it is both
    # "exclusion range already present" (Add-DhcpServerv4ExclusionRange) and
    # "failed to set IP address range to a scope" (Set-DhcpServerv4Scope). The
    # category is InvalidData for both, too broad to match on, so the code is
    # matched instead. Only ever pass ignore_already_exists=True to a cmdlet
    # where "already present" is the sole way 20023 can arise — never to a
    # range-setting call, or a real failure will be silently swallowed.
    "dhcp 20023",
    "already exists",
    "already been added",
    "already in use",
)


def is_not_found_error(stderr: str) -> bool:
    """Return True if PowerShell stderr indicates the requested object does not exist."""
    lower = stderr.lower()
    return any(kw in lower for kw in _NOT_FOUND_MARKERS)


def is_already_exists_error(stderr: str) -> bool:
    """Return True if PowerShell stderr indicates the object already exists."""
    lower = stderr.lower()
    return any(kw in lower for kw in _ALREADY_EXISTS_MARKERS)


async def run_ps(
    command: str,
    parse_json: bool = True,
    *,
    append_error_action: bool = True,
    append_convert_to_json: bool = True,
    scope: str | None = None,
    operation: str | None = None,
    relationship_name: str | None = None,
) -> dict | list | None:
    """Execute a PowerShell command and optionally parse JSON output.

    By default, appends -ErrorAction Stop so errors raise PowerShellError
    instead of silently returning empty output, and appends ConvertTo-Json for
    callers that want parsed JSON from a plain PowerShell object.

    Set append_error_action=False and append_convert_to_json=False only for
    complete scripts that already handle per-cmdlet errors and emit JSON.

    Execution-layer guard: validates DHCP environment before every call.
    This is a mandatory safety net — even if route-level protection is bypassed,
    DHCP operations will not proceed in unsupported environments.
    The validation result is cached so this check is free after the first call.

    Raises:
        DhcpEnvironmentError: if the runtime cannot support DHCP automation.
        PowerShellError: if the PowerShell command exits with a non-zero code.
    """
    await dhcp_service.validate_dhcp_environment()

    full_cmd = command
    if append_error_action:
        full_cmd = f"{full_cmd} -ErrorAction Stop"
    if parse_json and append_convert_to_json:
        full_cmd += " | ConvertTo-Json -Depth 5 -Compress"

    log_extra = {
        "scope": scope,
        "operation": operation or "powershell",
        "relationship_name": relationship_name,
    }
    logger.info("Running DHCP PowerShell command", extra=log_extra)

    t0 = time.monotonic()
    try:
        async with _get_powershell_semaphore():
            result = await get_transport().execute(
                full_cmd,
                settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError as exc:
        raise PowerShellTimeoutError(
            command,
            settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            operation=operation,
            scope=scope,
        ) from exc

    stdout = result.stdout
    stderr = result.stderr
    duration_ms = round((time.monotonic() - t0) * 1000, 2)

    if result.returncode != 0:
        logger.error(
            "DHCP PowerShell command failed",
            extra={
                **log_extra,
                "duration_ms": duration_ms,
                "status": "failed",
                "returncode": result.returncode,
                "stderr_preview": sanitize_powershell_text(stderr.strip()),
            },
        )
        raise PowerShellExecutionError(
            command,
            stderr.strip(),
            result.returncode or 1,
            operation=operation,
            scope=scope,
        )

    logger.info(
        "DHCP PowerShell command completed",
        extra={**log_extra, "duration_ms": duration_ms, "status": "ok"},
    )

    if not parse_json or not stdout.strip():
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PowerShellExecutionError(
            command,
            f"PowerShell returned non-JSON output: {exc}. stdout={stdout.strip()[:200]!r}",
            0,
            operation=operation,
            scope=scope,
        ) from exc
