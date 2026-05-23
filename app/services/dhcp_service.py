"""DHCP automation environment validator.

Validates that the current runtime is capable of executing DHCP operations
through Windows PowerShell. Two concerns are enforced here so no other module
needs ad-hoc environment awareness:

  1. OS / execution context  — native Windows only; WSL/Linux/macOS rejected.
  2. PowerShell availability — powershell.exe exists and can execute.
  3. DHCP cmdlet availability — the DhcpServer module cmdlets are discoverable.

Results are cached after the first call (async-safe). Callers only pay the
subprocess cost once per process lifetime.

For testing, call _reset_validation_cache() to clear the cache between tests.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import asyncio
import time

from app.config import settings
from app.errors import DhcpEnvReason, DhcpEnvironmentError
from app.utils.decorators import log_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level validation cache  (async-safe, set once per process)
# ---------------------------------------------------------------------------

_cache_lock: asyncio.Lock | None = None
_cache_lock_loop: asyncio.AbstractEventLoop | None = None
_cache_ok: bool | None = None
_cache_exc: DhcpEnvironmentError | None = None
_cache_negative_until: float = 0.0

# Transient startup failures (e.g. a brief PowerShell startup glitch) should not
# cause permanent 503s for the process lifetime. Re-check after this interval.
_NEGATIVE_CACHE_TTL_SECS: float = 30.0


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock, _cache_lock_loop

    loop = asyncio.get_running_loop()
    if _cache_lock is None or _cache_lock_loop is not loop:
        _cache_lock = asyncio.Lock()
        _cache_lock_loop = loop
    return _cache_lock


def _reset_validation_cache() -> None:
    """Reset the cached validation result.  For testing only."""
    global _cache_ok, _cache_exc, _cache_negative_until
    _cache_ok = None
    _cache_exc = None
    _cache_negative_until = 0.0


# ---------------------------------------------------------------------------
# Individual environment checks  (internal — testable in isolation)
# ---------------------------------------------------------------------------

def _is_wsl() -> bool:
    """Return True if this process is running inside WSL."""
    # WSL sets these env vars in every distro
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSLENV"):
        return True
    # /proc/version on WSL contains "Microsoft" or "WSL"
    try:
        with open("/proc/version") as fh:
            content = fh.read().lower()
            return "microsoft" in content or "wsl" in content
    except OSError:
        return False


def _check_os() -> None:
    """Raise DhcpEnvironmentError if not running on native Windows."""
    system = platform.system()

    if system == "Linux":
        if _is_wsl():
            raise DhcpEnvironmentError(
                DhcpEnvReason.WSL_DETECTED,
                "WSL (Windows Subsystem for Linux) is not a supported runtime. "
                "DHCP automation requires native Windows execution. "
                "Run this backend directly on a Windows DHCP server or a domain-joined Windows host "
                "— not inside WSL.",
            )
        raise DhcpEnvironmentError(
            DhcpEnvReason.UNSUPPORTED_OS,
            "Unsupported operating system: Linux. "
            "DHCP automation requires native Windows with the DhcpServer PowerShell module.",
        )

    if system == "Darwin":
        raise DhcpEnvironmentError(
            DhcpEnvReason.UNSUPPORTED_OS,
            "Unsupported operating system: macOS. "
            "DHCP automation requires native Windows with the DhcpServer PowerShell module.",
        )

    if system != "Windows":
        raise DhcpEnvironmentError(
            DhcpEnvReason.UNSUPPORTED_OS,
            f"Unsupported operating system: {system!r}. "
            "DHCP automation requires native Windows.",
        )


async def _run_powershell_check(command: str, timeout_seconds: int) -> tuple[int, str]:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise

    return process.returncode or 0, stderr.decode(errors="replace")


async def _check_powershell_binary() -> None:
    """Raise DhcpEnvironmentError if powershell.exe is missing or cannot execute.

    Requires Windows PowerShell (powershell.exe), not PowerShell 7 (pwsh.exe).
    The DhcpServer module ships with Windows PowerShell — pwsh availability
    alone does not imply DHCP cmdlet support.
    """
    ps_path = shutil.which("powershell")
    if ps_path is None:
        raise DhcpEnvironmentError(
            DhcpEnvReason.POWERSHELL_NOT_FOUND,
            "Windows PowerShell (powershell.exe) was not found on PATH. "
            "Install Windows PowerShell 5.1 or later.",
        )

    try:
        returncode, stderr = await _run_powershell_check(
            "exit 0",
            settings.POWERSHELL_ENV_CHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise DhcpEnvironmentError(
            DhcpEnvReason.POWERSHELL_EXEC_FAILED,
            f"Windows PowerShell found at {ps_path!r} but timed out during startup check. "
            "PowerShell may be hung or system resources exhausted.",
        )
    if returncode != 0:
        raise DhcpEnvironmentError(
            DhcpEnvReason.POWERSHELL_EXEC_FAILED,
            f"Windows PowerShell found at {ps_path!r} but failed to execute "
            f"(rc={returncode}). "
            f"stderr: {stderr.strip()!r}",
        )


async def _check_dhcp_cmdlets() -> None:
    """Raise DhcpEnvironmentError if the required DHCP cmdlets are not available.

    Uses Get-Command to check whether Get-DhcpServerv4Scope is discoverable.
    That cmdlet is representative of the entire DhcpServer module — if it is
    present, all other cmdlets used by this project are available.

    Deliberately avoids:
      - Get-WindowsFeature  (only available on Windows Server with ServerManager)
      - Get-Module -ListAvailable  (lists installed modules, not loaded commands)
      - Any actual DHCP query  (would require a configured server and permissions)

    This check runs fully local — no network access, no DHCP server required.
    """
    try:
        returncode, _stderr = await _run_powershell_check(
            "Get-Command Get-DhcpServerv4Scope -ErrorAction Stop | Out-Null",
            settings.POWERSHELL_ENV_CHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise DhcpEnvironmentError(
            DhcpEnvReason.POWERSHELL_EXEC_FAILED,
            "Timed out while checking for DHCP PowerShell cmdlets. "
            "PowerShell may be hung or system resources exhausted.",
        )
    if returncode != 0:
        raise DhcpEnvironmentError(
            DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE,
            "DHCP PowerShell cmdlets are not available on this machine. "
            "The command 'Get-DhcpServerv4Scope' was not found. "
            "Install the DhcpServer PowerShell module: "
            "on Windows Server, ensure the DHCP Server role is installed; "
            "on Windows client, install RSAT → Remote Server Administration Tools → DHCP Server Tools. "
            "Note: PowerShell alone does not imply DHCP cmdlet availability.",
        )


# ---------------------------------------------------------------------------
# Public validator
# ---------------------------------------------------------------------------

@log_call
async def validate_dhcp_environment() -> None:
    """Validate that this runtime can perform DHCP automation via Windows PowerShell.

    Checks (in order):
      1. Native Windows OS — Linux, macOS, and WSL are rejected with distinct messages.
      2. powershell.exe present and executable.
      3. Get-DhcpServerv4Scope discoverable (DhcpServer module available).

    Async-safe. Results are cached after the first call; subsequent calls return
    immediately from cache.  A failed environment is cached and will fail every
    future call until the process restarts.

    Raises:
        DhcpEnvironmentError: if any check fails.
    """
    global _cache_ok, _cache_exc, _cache_negative_until

    async with _get_cache_lock():
        if _cache_ok is True:
            return
        if _cache_exc is not None:
            if time.monotonic() < _cache_negative_until:
                raise _cache_exc
            # TTL expired — clear negative cache and re-validate.
            # Allows recovery from transient startup failures without process restart.
            logger.info("DHCP environment negative cache expired — re-validating")
            _cache_exc = None
            _cache_ok = None

        # Run all checks inside the lock to prevent duplicate PowerShell checks
        # under concurrent startup traffic.
        try:
            _check_os()
            await _check_powershell_binary()
            await _check_dhcp_cmdlets()
        except DhcpEnvironmentError as exc:
            logger.error(
                "DHCP environment validation failed [%s]: %s", exc.reason, exc.detail
            )
            _cache_ok = False
            _cache_exc = exc
            _cache_negative_until = time.monotonic() + _NEGATIVE_CACHE_TTL_SECS
            raise

        logger.info("DHCP environment validation passed — caching result")
        _cache_ok = True
        _cache_exc = None


@log_call
async def check_health() -> dict:
    await validate_dhcp_environment()
    return {"status": "ok"}
