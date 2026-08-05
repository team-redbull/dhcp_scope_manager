"""PSRP transport — executes PowerShell on a remote Windows DHCP server.

The API sends script text over WinRM (HTTPS, one port) and the Windows host
runs it in a real Windows PowerShell runspace with the ``DhcpServer`` module
loaded. Nothing PowerShell-related executes locally, so the API can run on
Linux while the DHCP server stays Windows.

Deserialization is a non-issue here: every command this codebase issues ends in
``ConvertTo-Json``, so the only thing crossing the wire is a string. The
parsing layer (``ps_parsers``) is untouched by the transport choice.

``pypsrp`` is synchronous, so every call into it runs via ``asyncio.to_thread``.
Runspaces are pooled because per-call PSRP session setup (TCP, TLS, auth,
runspace open) is far too slow to pay per request.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.errors import DhcpEnvReason, DhcpEnvironmentError
from app.services.ps_transport import PsResult

logger = logging.getLogger(__name__)

# pypsrp requires read_timeout > operation_timeout, otherwise the HTTP read can
# expire before the server has had a chance to report the operation timeout.
_READ_TIMEOUT_MARGIN_SECS = 10

# System.Management.Automation.ErrorCategory. PSRP transmits the category as an
# integer; console PowerShell prints the name. Only the values this project
# classifies on are mapped by name — anything else falls back to the number,
# which is still better than dropping it.
_ERROR_CATEGORY_NAMES = {
    5: "InvalidArgument",
    6: "InvalidData",
    7: "InvalidOperation",
    13: "ObjectNotFound",
    18: "PermissionDenied",
    20: "ResourceExists",
    21: "ResourceUnavailable",
    28: "AuthenticationError",
}


def _format_error_record(err: object) -> str:
    """Render a PSRP ErrorRecord the way console PowerShell writes it to stderr.

    This is a parity requirement, not cosmetics. The local transport's stderr
    carries CategoryInfo and FullyQualifiedErrorId lines, and error
    classification in ps_executor depends on them: real DhcpServer messages
    ("Failed to get scope information for scope X") contain no not-found wording
    at all, so the category is the only reliable signal. Returning just the
    message here would make the same DHCP error classify differently depending
    on which transport ran it.
    """
    message = str(getattr(err, "message", "") or "").strip()
    if not message:
        exception = getattr(err, "exception", None)
        message = str(getattr(exception, "Message", "") or exception or err).strip()

    lines = [message]

    category = getattr(err, "category", None)
    if category is not None:
        name = _ERROR_CATEGORY_NAMES.get(category, str(category))
        lines.append(f"    + CategoryInfo          : {name}")

    fq_error = getattr(err, "fq_error", None)
    if fq_error:
        lines.append(f"    + FullyQualifiedErrorId : {fq_error}")

    return "\n".join(lines)


def _import_pypsrp():
    """Import pypsrp lazily so the local transport never requires it installed."""
    try:
        from pypsrp.powershell import PowerShell, RunspacePool
        from pypsrp.wsman import WSMan
    except ImportError as exc:
        raise DhcpEnvironmentError(
            DhcpEnvReason.PSRP_DEPENDENCY_MISSING,
            "DHCP_TRANSPORT='psrp' requires the 'pypsrp' package, which is not "
            "installed. Install it (pip install -r requirements.txt) or set "
            "DHCP_TRANSPORT='local' to run PowerShell on this host.",
        ) from exc
    return WSMan, RunspacePool, PowerShell


def _connection_kwargs() -> dict:
    """Build WSMan connection arguments for the configured auth mode.

    Kerberos deliberately passes no credentials — it authenticates from the
    host keytab or credential cache, so no password is ever stored or logged.

    CredSSP passes them like ntlm, but the protocol additionally *delegates*
    the credential to the DHCP server, which is what lets the failover cmdlets
    authenticate onward to the partner. See the WINRM_AUTH notes in config.py.
    """
    kwargs = {
        "server": settings.DHCP_SERVER_HOST,
        "port": settings.WINRM_PORT,
        "ssl": settings.WINRM_USE_SSL,
        "auth": settings.WINRM_AUTH,
        "cert_validation": settings.WINRM_CERT_VALIDATION,
        "connection_timeout": settings.WINRM_CONNECTION_TIMEOUT_SECONDS,
        "operation_timeout": settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS,
        "read_timeout": settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS + _READ_TIMEOUT_MARGIN_SECS,
    }
    if settings.WINRM_AUTH in ("ntlm", "credssp"):
        kwargs["username"] = settings.WINRM_USERNAME
        kwargs["password"] = settings.WINRM_PASSWORD
    elif settings.WINRM_USERNAME:
        # Kerberos with an explicit principal (rather than the default ccache).
        kwargs["username"] = settings.WINRM_USERNAME
    return kwargs


class _Runspace:
    """One open PSRP runspace pool.

    Fully synchronous — every method is invoked through ``asyncio.to_thread``
    and a given instance is only ever used by one task at a time, enforced by
    the checkout/checkin discipline in :class:`PsrpTransport`.
    """

    def __init__(self) -> None:
        self._pool = None

    def open(self) -> None:
        WSMan, RunspacePool, _ = _import_pypsrp()
        try:
            wsman = WSMan(**_connection_kwargs())
            pool = RunspacePool(wsman)
            pool.open()
        except DhcpEnvironmentError:
            raise
        except Exception as exc:
            raise DhcpEnvironmentError(
                DhcpEnvReason.PSRP_CONNECTION_FAILED,
                f"Could not open a PowerShell session on DHCP server "
                f"{settings.DHCP_SERVER_HOST!r} over WinRM "
                f"(port={settings.WINRM_PORT}, auth={settings.WINRM_AUTH}): {exc}",
            ) from exc
        self._pool = pool

    def run(self, script: str) -> PsResult:
        _, _, PowerShell = _import_pypsrp()

        ps = PowerShell(self._pool)
        ps.add_script(script)
        output = ps.invoke()

        stdout = "\n".join(str(item) for item in output if item is not None)

        # run_ps appends -ErrorAction Stop, so any error record is terminating.
        # Surfacing it as a non-zero result keeps PowerShellExecutionError and
        # the is_not_found_error / is_already_exists_error matching identical
        # to the local transport.
        errors = list(getattr(ps.streams, "error", []) or [])
        if ps.had_errors or errors:
            return PsResult(
                returncode=1,
                stdout=stdout,
                stderr="\n".join(_format_error_record(err) for err in errors),
            )
        return PsResult(returncode=0, stdout=stdout, stderr="")

    def close(self) -> None:
        if self._pool is None:
            return
        try:
            self._pool.close()
        except Exception:
            logger.debug("Error closing PSRP runspace", exc_info=True)
        finally:
            self._pool = None


class PsrpTransport:
    """Pooled PSRP transport.

    The pool is not explicitly sized: ``run_ps`` already gates every call
    behind a semaphore of ``POWERSHELL_MAX_CONCURRENCY``, so at most that many
    runspaces are ever live. Idle runspaces are reused LIFO to keep the
    working set small when traffic drops.
    """

    def __init__(self) -> None:
        self._idle: asyncio.LifoQueue | None = None
        self._idle_loop: asyncio.AbstractEventLoop | None = None

    def _get_idle(self) -> asyncio.LifoQueue:
        """Return the idle pool bound to the running loop.

        Mirrors the semaphore handling in ``ps_executor`` — pytest and ASGI
        servers may use different event loops over the process lifetime.
        """
        loop = asyncio.get_running_loop()
        if self._idle is None or self._idle_loop is not loop:
            self._idle = asyncio.LifoQueue()
            self._idle_loop = loop
        return self._idle

    async def execute(self, script: str, timeout_seconds: int) -> PsResult:
        idle = self._get_idle()

        try:
            runspace = idle.get_nowait()
        except asyncio.QueueEmpty:
            runspace = _Runspace()
            await asyncio.to_thread(runspace.open)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(runspace.run, script),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            # wait_for cannot cancel the worker thread — it may still be blocked
            # in WinRM I/O. The runspace is no longer in a known state, so it is
            # discarded rather than returned to the pool, and closed in the
            # background so the timeout surfaces to the caller immediately.
            asyncio.get_running_loop().run_in_executor(None, runspace.close)
            raise
        except Exception:
            await asyncio.to_thread(runspace.close)
            raise

        idle.put_nowait(runspace)
        return result

    async def aclose(self) -> None:
        """Close every idle runspace.  Called on application shutdown."""
        if self._idle is None:
            return
        while True:
            try:
                runspace = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            await asyncio.to_thread(runspace.close)
