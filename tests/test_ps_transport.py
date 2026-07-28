"""Tests for the PowerShell transport layer (local subprocess and PSRP).

The PSRP tests never touch a network or require pypsrp to be installed —
_import_pypsrp is patched with fakes that mimic the pypsrp object model
(RunspacePool / PowerShell / streams.error).
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import ValidationError
from unittest.mock import AsyncMock, patch

from app.config import Settings, settings
from app.errors import DhcpEnvReason, DhcpEnvironmentError
from app.services import psrp_pool
# Imported at module scope so these are the real functions: the autouse fixture
# in conftest.py patches dhcp_service.validate_dhcp_environment, and a late
# import inside a test would bind that mock instead.
from app.services.dhcp_service import (
    _check_psrp_target,
    _reset_validation_cache,
    validate_dhcp_environment,
)
from app.services.ps_transport import (
    LocalSubprocessTransport,
    PsResult,
    _reset_transport,
    get_transport,
)
from app.services.psrp_pool import PsrpTransport, _connection_kwargs, _Runspace


def _psrp_settings(**overrides):
    """Patch the global settings onto a valid psrp configuration."""
    values = {
        "DHCP_TRANSPORT": "psrp",
        "DHCP_SERVER_HOST": "dhcp01.lab.local",
        "WINRM_PORT": 5986,
        "WINRM_USE_SSL": True,
        "WINRM_AUTH": "kerberos",
        "WINRM_USERNAME": "",
        "WINRM_PASSWORD": "",
        "WINRM_CERT_VALIDATION": True,
        "WINRM_CONNECTION_TIMEOUT_SECONDS": 30,
        **overrides,
    }
    return patch.multiple(settings, **values)


@pytest.fixture(autouse=True)
def _clean_transport_cache():
    _reset_transport()
    yield
    _reset_transport()


# ─── Settings validation ──────────────────────────────────────────────────────

class TestTransportSettings:

    def test_local_is_default_and_needs_no_target(self):
        s = Settings(_env_file=None)
        assert s.DHCP_TRANSPORT == "local"
        assert s.is_psrp is False

    def test_psrp_requires_dhcp_server_host(self):
        with pytest.raises(ValidationError, match="DHCP_SERVER_HOST is required"):
            Settings(_env_file=None, DHCP_TRANSPORT="psrp")

    def test_psrp_with_host_is_valid(self):
        s = Settings(_env_file=None, DHCP_TRANSPORT="psrp", DHCP_SERVER_HOST="dhcp01")
        assert s.is_psrp is True
        assert s.WINRM_PORT == 5986
        assert s.WINRM_AUTH == "kerberos"

    def test_ntlm_without_credentials_rejected(self):
        with pytest.raises(ValidationError, match="WINRM_USERNAME and WINRM_PASSWORD"):
            Settings(
                _env_file=None,
                DHCP_TRANSPORT="psrp",
                DHCP_SERVER_HOST="dhcp01",
                WINRM_AUTH="ntlm",
            )

    def test_ntlm_with_credentials_accepted(self):
        s = Settings(
            _env_file=None,
            DHCP_TRANSPORT="psrp",
            DHCP_SERVER_HOST="dhcp01",
            WINRM_AUTH="ntlm",
            WINRM_USERNAME="svc",
            WINRM_PASSWORD="pw",
        )
        assert s.WINRM_AUTH == "ntlm"

    def test_unknown_auth_rejected(self):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                DHCP_TRANSPORT="psrp",
                DHCP_SERVER_HOST="dhcp01",
                WINRM_AUTH="basic",
            )


# ─── Transport selection ──────────────────────────────────────────────────────

class TestTransportFactory:

    def test_local_transport_selected_by_default(self):
        with patch.object(settings, "DHCP_TRANSPORT", "local"):
            assert isinstance(get_transport(), LocalSubprocessTransport)

    def test_psrp_transport_selected_when_configured(self):
        with _psrp_settings():
            assert isinstance(get_transport(), PsrpTransport)

    def test_transport_is_cached(self):
        with patch.object(settings, "DHCP_TRANSPORT", "local"):
            assert get_transport() is get_transport()

    def test_transport_rebuilt_when_mode_changes(self):
        with patch.object(settings, "DHCP_TRANSPORT", "local"):
            local = get_transport()
        with _psrp_settings():
            remote = get_transport()
        assert local is not remote
        assert isinstance(remote, PsrpTransport)


# ─── Local subprocess transport ───────────────────────────────────────────────

class _FakeProcess:
    def __init__(self, *, returncode=0, stdout=b"", stderr=b"", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(30)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestLocalSubprocessTransport:
    pytestmark = pytest.mark.asyncio

    async def test_returns_decoded_result(self):
        proc = _FakeProcess(returncode=0, stdout=b'{"a":1}', stderr=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await LocalSubprocessTransport().execute("Get-Item", 30)
        assert result == PsResult(returncode=0, stdout='{"a":1}', stderr="")

    async def test_non_utf8_bytes_decoded_with_replacement(self):
        proc = _FakeProcess(returncode=1, stderr=b"\x80\x81 denied")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await LocalSubprocessTransport().execute("Get-Item", 30)
        assert result.returncode == 1
        assert isinstance(result.stderr, str)

    async def test_timeout_kills_process_and_raises(self):
        proc = _FakeProcess(hang=True)
        proc.returncode = None
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(asyncio.TimeoutError):
                await LocalSubprocessTransport().execute("Get-Item", 0.05)
        assert proc.killed


# ─── PSRP connection arguments ────────────────────────────────────────────────

class TestConnectionKwargs:

    def test_kerberos_sends_no_password(self):
        with _psrp_settings(WINRM_AUTH="kerberos"):
            kwargs = _connection_kwargs()
        assert kwargs["auth"] == "kerberos"
        assert "password" not in kwargs
        assert kwargs["server"] == "dhcp01.lab.local"
        assert kwargs["port"] == 5986

    def test_ntlm_sends_credentials(self):
        with _psrp_settings(
            WINRM_AUTH="ntlm", WINRM_USERNAME="svc", WINRM_PASSWORD="pw"
        ):
            kwargs = _connection_kwargs()
        assert kwargs["username"] == "svc"
        assert kwargs["password"] == "pw"

    def test_read_timeout_exceeds_operation_timeout(self):
        """pypsrp requires read_timeout > operation_timeout."""
        with _psrp_settings():
            kwargs = _connection_kwargs()
        assert kwargs["read_timeout"] > kwargs["operation_timeout"]


# ─── Fake pypsrp object model ─────────────────────────────────────────────────

class _FakeStreams:
    def __init__(self, errors):
        self.error = list(errors)


class _FakePowerShell:
    """Mimics pypsrp.powershell.PowerShell."""

    def __init__(self, pool, *, output=None, errors=(), raises=None):
        self.pool = pool
        self._output = output if output is not None else []
        self.streams = _FakeStreams(errors)
        self.had_errors = bool(errors)
        self._raises = raises
        self.script = None

    def add_script(self, script):
        self.script = script

    def invoke(self):
        if self._raises:
            raise self._raises
        return self._output


class _FakeRunspacePool:
    def __init__(self, wsman, *, open_raises=None):
        self.wsman = wsman
        self.opened = False
        self.closed = False
        self._open_raises = open_raises

    def open(self):
        if self._open_raises:
            raise self._open_raises
        self.opened = True

    def close(self):
        self.closed = True


def _fake_pypsrp(*, output=None, errors=(), open_raises=None, invoke_raises=None):
    """Return a replacement for _import_pypsrp() backed by the fakes above."""
    def _wsman(**kwargs):
        return kwargs

    def _pool(wsman):
        return _FakeRunspacePool(wsman, open_raises=open_raises)

    def _ps(pool):
        return _FakePowerShell(pool, output=output, errors=errors, raises=invoke_raises)

    return lambda: (_wsman, _pool, _ps)


# ─── _Runspace result mapping ─────────────────────────────────────────────────

class TestRunspaceResultMapping:

    def test_clean_invoke_maps_to_returncode_zero(self):
        with _psrp_settings(), patch.object(
            psrp_pool, "_import_pypsrp", _fake_pypsrp(output=['{"ScopeId":"10.20.30.0"}'])
        ):
            rs = _Runspace()
            rs.open()
            result = rs.run("Get-DhcpServerv4Scope")
        assert result.returncode == 0
        assert result.stdout == '{"ScopeId":"10.20.30.0"}'
        assert result.stderr == ""

    def test_error_stream_maps_to_returncode_one(self):
        with _psrp_settings(), patch.object(
            psrp_pool, "_import_pypsrp", _fake_pypsrp(errors=["Cannot find scope 10.20.30.0"])
        ):
            rs = _Runspace()
            rs.open()
            result = rs.run("Get-DhcpServerv4Scope")
        assert result.returncode == 1
        assert "Cannot find scope" in result.stderr

    def test_error_text_preserved_for_not_found_classification(self):
        """The stderr string must stay matchable by is_not_found_error."""
        from app.services.ps_executor import is_not_found_error

        with _psrp_settings(), patch.object(
            psrp_pool, "_import_pypsrp", _fake_pypsrp(errors=["The scope does not exist"])
        ):
            rs = _Runspace()
            rs.open()
            result = rs.run("Get-DhcpServerv4Scope")
        assert is_not_found_error(result.stderr)

    def test_multiline_output_joined(self):
        with _psrp_settings(), patch.object(
            psrp_pool, "_import_pypsrp", _fake_pypsrp(output=["line1", "line2"])
        ):
            rs = _Runspace()
            rs.open()
            result = rs.run("Get-Thing")
        assert result.stdout == "line1\nline2"

    def test_open_failure_becomes_connection_error(self):
        with _psrp_settings(), patch.object(
            psrp_pool,
            "_import_pypsrp",
            _fake_pypsrp(open_raises=OSError("connection refused")),
        ):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _Runspace().open()
        assert exc_info.value.reason == DhcpEnvReason.PSRP_CONNECTION_FAILED
        assert "dhcp01.lab.local" in exc_info.value.detail

    def test_missing_pypsrp_reports_dependency_error(self):
        real = psrp_pool._import_pypsrp
        with patch.dict("sys.modules", {"pypsrp": None, "pypsrp.wsman": None}):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                real()
        assert exc_info.value.reason == DhcpEnvReason.PSRP_DEPENDENCY_MISSING


# ─── PsrpTransport pooling ────────────────────────────────────────────────────

class _StubRunspace:
    instances: list["_StubRunspace"] = []

    def __init__(self, *, run_raises=None, block: threading.Event | None = None):
        self.opened = False
        self.closed = False
        self.runs = 0
        self._run_raises = run_raises
        self._block = block
        _StubRunspace.instances.append(self)

    def open(self):
        self.opened = True

    def run(self, script):
        self.runs += 1
        if self._block is not None:
            self._block.wait(timeout=5)
        if self._run_raises:
            raise self._run_raises
        return PsResult(returncode=0, stdout="ok", stderr="")

    def close(self):
        self.closed = True


@pytest.fixture
def _stub_runspaces():
    _StubRunspace.instances = []
    yield _StubRunspace
    _StubRunspace.instances = []


class TestPsrpTransportPooling:
    pytestmark = pytest.mark.asyncio

    async def test_first_call_opens_a_runspace(self, _stub_runspaces):
        with patch.object(psrp_pool, "_Runspace", _StubRunspace):
            result = await PsrpTransport().execute("Get-Item", 30)
        assert result.stdout == "ok"
        assert len(_StubRunspace.instances) == 1
        assert _StubRunspace.instances[0].opened

    async def test_idle_runspace_is_reused(self, _stub_runspaces):
        transport = PsrpTransport()
        with patch.object(psrp_pool, "_Runspace", _StubRunspace):
            await transport.execute("Get-Item", 30)
            await transport.execute("Get-Item", 30)
        assert len(_StubRunspace.instances) == 1, "second call must reuse the pooled runspace"
        assert _StubRunspace.instances[0].runs == 2

    async def test_failed_runspace_is_closed_and_not_reused(self, _stub_runspaces):
        boom = RuntimeError("session broken")
        transport = PsrpTransport()
        with patch.object(
            psrp_pool, "_Runspace", lambda: _StubRunspace(run_raises=boom)
        ):
            with pytest.raises(RuntimeError):
                await transport.execute("Get-Item", 30)
        assert _StubRunspace.instances[0].closed

        # Next call must build a fresh runspace rather than reuse the broken one.
        with patch.object(psrp_pool, "_Runspace", _StubRunspace):
            await transport.execute("Get-Item", 30)
        assert len(_StubRunspace.instances) == 2

    async def test_timeout_discards_runspace(self, _stub_runspaces):
        gate = threading.Event()
        transport = PsrpTransport()
        try:
            with patch.object(psrp_pool, "_Runspace", lambda: _StubRunspace(block=gate)):
                with pytest.raises(asyncio.TimeoutError):
                    await transport.execute("Get-Item", 0.05)
        finally:
            gate.set()

        # Discarded, not returned to the idle pool.
        with patch.object(psrp_pool, "_Runspace", _StubRunspace):
            await transport.execute("Get-Item", 30)
        assert len(_StubRunspace.instances) == 2

    async def test_aclose_closes_idle_runspaces(self, _stub_runspaces):
        transport = PsrpTransport()
        with patch.object(psrp_pool, "_Runspace", _StubRunspace):
            await transport.execute("Get-Item", 30)
        await transport.aclose()
        assert _StubRunspace.instances[0].closed


# ─── Remote environment check ─────────────────────────────────────────────────

class TestCheckPsrpTarget:
    pytestmark = pytest.mark.asyncio

    async def _run_with_transport(self, execute):
        fake = type("T", (), {"execute": staticmethod(execute)})()
        with _psrp_settings(), patch(
            "app.services.ps_transport.get_transport", return_value=fake
        ):
            await _check_psrp_target()

    async def test_passes_when_cmdlets_present(self):
        async def execute(script, timeout):
            assert "Get-DhcpServerv4Scope" in script
            return PsResult(returncode=0, stdout="", stderr="")

        await self._run_with_transport(execute)  # must not raise

    async def test_missing_remote_cmdlets_reported(self):
        async def execute(script, timeout):
            return PsResult(returncode=1, stdout="", stderr="not recognized")

        with pytest.raises(DhcpEnvironmentError) as exc_info:
            await self._run_with_transport(execute)
        assert exc_info.value.reason == DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE
        assert "dhcp01.lab.local" in exc_info.value.detail

    async def test_timeout_reported_as_exec_failure(self):
        async def execute(script, timeout):
            raise asyncio.TimeoutError

        with pytest.raises(DhcpEnvironmentError) as exc_info:
            await self._run_with_transport(execute)
        assert exc_info.value.reason == DhcpEnvReason.POWERSHELL_EXEC_FAILED

    async def test_connection_error_propagates_unchanged(self):
        async def execute(script, timeout):
            raise DhcpEnvironmentError(
                DhcpEnvReason.PSRP_CONNECTION_FAILED, "kerberos failed"
            )

        with pytest.raises(DhcpEnvironmentError) as exc_info:
            await self._run_with_transport(execute)
        assert exc_info.value.reason == DhcpEnvReason.PSRP_CONNECTION_FAILED


# ─── run_ps over the psrp transport ───────────────────────────────────────────

class TestRunPsOverPsrp:
    """run_ps must behave identically regardless of which transport ran.

    This is the guarantee the whole migration rests on: only the transport
    changes, and every parsing/canonicalisation rule above it is untouched.
    """

    pytestmark = pytest.mark.asyncio

    async def _run(self, output, **run_ps_kwargs):
        from app.services.ps_executor import run_ps

        class _Stub:
            def __init__(self):
                self.script = None

            def open(self):
                pass

            def run(self, script):
                self.script = script
                return PsResult(returncode=0, stdout=output, stderr="")

            def close(self):
                pass

        stub = _Stub()
        with _psrp_settings(), patch.object(psrp_pool, "_Runspace", lambda: stub):
            result = await run_ps("Get-DhcpServerv4Scope", **run_ps_kwargs)
        return result, stub

    async def test_json_parsed_from_remote_output(self):
        result, _ = await self._run('{"ScopeId":"10.20.30.0","Name":"cluster-a"}')
        assert result == {"ScopeId": "10.20.30.0", "Name": "cluster-a"}

    async def test_ipaddress_dicts_survive_the_transport(self):
        """.NET IPAddress fields still arrive as IPAddressToString dicts.

        _extract_ip_str depends on this shape (CLAUDE.md section 8), so it must
        hold over PSRP exactly as it does for a local subprocess.
        """
        from app.services.ps_parsers import _extract_ip_str

        raw = '{"SubnetMask":{"IPAddressToString":"255.255.255.0"}}'
        result, _ = await self._run(raw)
        assert _extract_ip_str(result["SubnetMask"]) == "255.255.255.0"

    async def test_error_action_and_convert_to_json_are_appended(self):
        _, stub = await self._run("null")
        assert "-ErrorAction Stop" in stub.script
        assert "ConvertTo-Json" in stub.script

    async def test_remote_error_raises_powershell_execution_error(self):
        from app.errors import PowerShellExecutionError
        from app.services.ps_executor import run_ps

        class _Stub:
            def open(self):
                pass

            def run(self, script):
                return PsResult(returncode=1, stdout="", stderr="Access is denied")

            def close(self):
                pass

        with _psrp_settings(), patch.object(psrp_pool, "_Runspace", _Stub):
            with pytest.raises(PowerShellExecutionError) as exc_info:
                await run_ps("Get-DhcpServerv4Scope")
        assert "Access is denied" in exc_info.value.stderr

    async def test_remote_timeout_raises_powershell_timeout_error(self):
        from app.errors import PowerShellTimeoutError
        from app.services.ps_executor import run_ps

        gate = threading.Event()

        class _Stub:
            def open(self):
                pass

            def run(self, script):
                gate.wait(timeout=5)
                return PsResult(returncode=0, stdout="null", stderr="")

            def close(self):
                pass

        try:
            with _psrp_settings(), \
                 patch.object(settings, "POWERSHELL_COMMAND_TIMEOUT_SECONDS", 0.05), \
                 patch.object(psrp_pool, "_Runspace", _Stub):
                with pytest.raises(PowerShellTimeoutError):
                    await run_ps("Get-DhcpServerv4Scope")
        finally:
            gate.set()


# ─── Validator dispatch ───────────────────────────────────────────────────────

class TestValidatorDispatch:
    pytestmark = pytest.mark.asyncio

    async def test_psrp_mode_skips_local_os_check(self):
        """The whole point of psrp: a non-Windows host must validate fine."""
        _reset_validation_cache()
        with _psrp_settings(), \
             patch("platform.system", return_value="Linux"), \
             patch(
                 "app.services.dhcp_service._check_psrp_target", new=AsyncMock()
             ) as remote_check, \
             patch("app.services.dhcp_service._check_os") as os_check:
            await validate_dhcp_environment()

        remote_check.assert_awaited_once()
        os_check.assert_not_called()
        _reset_validation_cache()

    async def test_local_mode_still_runs_os_check(self):
        _reset_validation_cache()
        with patch.object(settings, "DHCP_TRANSPORT", "local"), \
             patch("app.services.dhcp_service._check_os") as os_check, \
             patch("app.services.dhcp_service._check_powershell_binary", new=AsyncMock()), \
             patch("app.services.dhcp_service._check_dhcp_cmdlets", new=AsyncMock()), \
             patch("app.services.dhcp_service._check_psrp_target", new=AsyncMock()) as remote_check:
            await validate_dhcp_environment()

        os_check.assert_called_once()
        remote_check.assert_not_called()
        _reset_validation_cache()
