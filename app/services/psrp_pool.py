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
import time

from app.config import settings
from app.errors import DhcpEnvReason, DhcpEnvironmentError, sanitize_powershell_text
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

        Rebinding *closes* what the previous loop left parked. Dropping the queue
        alone frees only this side: the runspace object goes out of scope, while
        the server keeps its half — a wsmprovhost.exe holding ~120 MB — until the
        WinRM IdleTimeout expires, which defaults to two hours. A deployed API
        never reaches this branch, since an ASGI server keeps one loop for the
        process lifetime; pytest-asyncio gives every test its own, so the live
        suite stranded one shell per test and exhausted a 2 GB DHCP server
        mid-run. Closing here is what makes that suite runnable end to end.
        """
        loop = asyncio.get_running_loop()
        if self._idle is None or self._idle_loop is not loop:
            if self._idle is not None:
                self._discard_idle(self._idle)
            self._idle = asyncio.LifoQueue()
            self._idle_loop = loop
        return self._idle

    def _checkin(self, idle: asyncio.LifoQueue, runspace: "_Runspace") -> None:
        """Return a runspace to the pool, stamped with the time it went idle.

        The timestamp lives here rather than on ``_Runspace`` because it is a
        property of the *caching*, not of the session: how long this process is
        willing to trust a cached runspace is the pool's policy alone.
        """
        idle.put_nowait((time.monotonic(), runspace))

    def _checkout(self, idle: asyncio.LifoQueue) -> "tuple[_Runspace, float] | None":
        """Take a reusable runspace from the pool, discarding expired ones.

        Returns the runspace and how long it sat idle, or ``None`` when nothing
        usable is left and the caller should open a fresh one. The queue is
        LIFO, so the first item out is the most recently used: once that one is
        too old, everything behind it is older still, and the loop drains the
        pool.
        """
        loop = asyncio.get_running_loop()
        max_idle = settings.WINRM_RUNSPACE_MAX_IDLE_SECONDS
        now = time.monotonic()

        while True:
            try:
                last_used, runspace = idle.get_nowait()
            except asyncio.QueueEmpty:
                return None

            idle_for = now - last_used
            if idle_for < max_idle:
                return runspace, idle_for

            # Past the point where the server is likely to still hold its half
            # open. Closed in the background so draining a stale pool does not
            # add its round trips to this request's latency.
            loop.run_in_executor(None, runspace.close)

    def _discard_idle(self, idle: asyncio.LifoQueue) -> None:
        """Drain a queue, closing every runspace still parked in it.

        Called from two places. After a pooled runspace has proved dead: LIFO
        means everything still queued was used *earlier* than the one that just
        failed, so if that one's session was gone, theirs is too — without this,
        a burst of requests arriving after an idle gap would each pay the same
        failure and retry in turn. And from ``_get_idle`` when the event loop
        changes, where the queue itself is being replaced and its contents would
        otherwise be stranded on the server.

        Closes run in the background for the same reason in both cases: draining
        a stale pool must not add its round trips to the caller's latency. The
        runspaces are closed rather than merely dropped because a dropped one
        keeps its server-side shell alive until WinRM's IdleTimeout.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                _, stale = idle.get_nowait()
            except asyncio.QueueEmpty:
                return
            loop.run_in_executor(None, stale.close)

    async def _run_on(
        self, runspace: "_Runspace", script: str, timeout_seconds: int
    ) -> PsResult:
        """Run one script on one runspace, closing it on any failure."""
        try:
            return await asyncio.wait_for(
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

    async def execute(self, script: str, timeout_seconds: int) -> PsResult:
        """Run a script, retrying once if a *pooled* runspace turns out to be dead.

        A cached runspace can be dead through no fault of this request: the
        server reaps its half of an idle WinRM session (HTTP.SYS keep-alive, the
        WinRM IdleTimeout, or the CredSSP context lifetime, whichever is
        shortest), and the next command on it gets a 401 that re-authentication
        cannot recover. See WINRM_RUNSPACE_MAX_IDLE_SECONDS in config.py.

        The retry is deliberately narrow. It fires only for a runspace taken
        from the pool — a freshly opened one failing is a real error, and
        retrying it would just double every genuine failure. A timeout never
        retries, since the script may still be running on the server.

        PowerShell *script* errors do not reach here: ``run`` returns them as
        ``PsResult(returncode=1)``, so any exception it raises is transport
        level and the script did not produce a result. It may still have
        executed — a session that died mid-Receive rather than on the opening
        401 would re-run the script on retry — which is safe here only because
        every cmdlet path this transport carries is convergence-idempotent
        (CLAUDE.md §2).
        """
        idle = self._get_idle()

        checked_out = self._checkout(idle)
        if checked_out is not None:
            runspace, idle_for = checked_out
            try:
                result = await self._run_on(runspace, script, timeout_seconds)
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                # Sanitized like any other transport text: a pypsrp/requests
                # exception can carry a connection repr, and that repr carries
                # the WinRM credential (CLAUDE.md §10, §12).
                logger.warning(
                    "Pooled PSRP runspace failed after %.0fs idle; retrying on a "
                    "fresh session. Lower WINRM_RUNSPACE_MAX_IDLE_SECONDS (now "
                    "%ds) if this repeats: %s",
                    idle_for,
                    settings.WINRM_RUNSPACE_MAX_IDLE_SECONDS,
                    sanitize_powershell_text(str(exc)),
                )
                self._discard_idle(idle)
            else:
                self._checkin(idle, runspace)
                return result

        # Reached either because the pool held nothing usable, or because a
        # pooled runspace just proved dead and this is the single retry.
        runspace = _Runspace()
        await asyncio.to_thread(runspace.open)
        result = await self._run_on(runspace, script, timeout_seconds)
        self._checkin(idle, runspace)
        return result

    async def aclose(self) -> None:
        """Close every idle runspace.  Called on application shutdown."""
        if self._idle is None:
            return
        while True:
            try:
                _, runspace = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            await asyncio.to_thread(runspace.close)
