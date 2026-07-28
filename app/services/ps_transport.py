"""PowerShell execution transports.

Separates *how* a PowerShell script reaches a Windows host from *what*
``run_ps`` does with the result. Everything above this layer — JSON parsing,
``_extract_ip_str``, error classification, the canonical payload shape — is
transport-agnostic and unchanged.

Two transports:

``local``
    Spawns ``powershell.exe`` as a subprocess. The API must itself run on a
    Windows host with the DHCP Server role.

``psrp``
    Sends script text to a remote Windows DHCP server over WinRM/PSRP. The
    cmdlets execute in a real Windows runspace on that server; no PowerShell
    runs locally, so the API can run on Linux.

Both return a :class:`PsResult` with the same shape, and both raise
``asyncio.TimeoutError`` on timeout so ``run_ps`` can map it to
``PowerShellTimeoutError`` without knowing which transport ran.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PsResult:
    """Outcome of one PowerShell execution, independent of transport.

    ``returncode`` is 0 on success and non-zero on failure. For PSRP there is
    no process exit code, so an error stream is normalised to 1 — this keeps
    the ``is_not_found_error`` / ``is_already_exists_error`` string matching in
    ``ps_executor`` working identically across transports.
    """

    returncode: int
    stdout: str
    stderr: str


class PowerShellTransport(Protocol):
    async def execute(self, script: str, timeout_seconds: int) -> PsResult: ...


class LocalSubprocessTransport:
    """Runs PowerShell as a local ``powershell.exe`` subprocess."""

    async def execute(self, script: str, timeout_seconds: int) -> PsResult:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise

        return PsResult(
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )


_transport: PowerShellTransport | None = None
_transport_kind: str | None = None


def get_transport() -> PowerShellTransport:
    """Return the transport for the configured mode, created once per process."""
    global _transport, _transport_kind

    if _transport is None or _transport_kind != settings.DHCP_TRANSPORT:
        if settings.is_psrp:
            from app.services.psrp_pool import PsrpTransport

            _transport = PsrpTransport()
            logger.info(
                "PowerShell transport: psrp → %s:%s (auth=%s)",
                settings.DHCP_SERVER_HOST,
                settings.WINRM_PORT,
                settings.WINRM_AUTH,
            )
        else:
            _transport = LocalSubprocessTransport()
            logger.info("PowerShell transport: local subprocess")
        _transport_kind = settings.DHCP_TRANSPORT

    return _transport


def _reset_transport() -> None:
    """Drop the cached transport.  For testing only."""
    global _transport, _transport_kind
    _transport = None
    _transport_kind = None
