"""Tests for DHCP environment validation layer.

Each test that exercises validate_dhcp_environment() or its helpers calls
_reset_validation_cache() first so that the module-level cache never bleeds
between tests.  The autouse fixture in conftest.py patches validate_dhcp_environment
to a no-op for all other test files; tests here intentionally override that patch
when they need to exercise real validation or check route/execution-layer behaviour.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.errors import DhcpEnvironmentError, DhcpEnvReason
from app.services.dhcp_service import (
    _check_dhcp_cmdlets,
    _check_os,
    _check_powershell_binary,
    _reset_validation_cache,
    validate_dhcp_environment,
)

async def _request(method: str, path: str, **kwargs):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _error(body):
    return body["error"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_error(reason: str, detail: str = "test detail") -> DhcpEnvironmentError:
    return DhcpEnvironmentError(reason, detail)


# ---------------------------------------------------------------------------
# _check_os — unit tests for the OS / WSL detector
# ---------------------------------------------------------------------------

class TestCheckOs:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_windows_passes(self):
        with patch("platform.system", return_value="Windows"):
            _check_os()  # must not raise

    async def test_linux_rejected(self):
        with patch("platform.system", return_value="Linux"), \
             patch("app.services.dhcp_service._is_wsl", return_value=False):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_os()
        assert exc_info.value.reason == DhcpEnvReason.UNSUPPORTED_OS
        assert "Linux" in exc_info.value.detail

    async def test_wsl_rejected_with_distinct_reason(self):
        with patch("platform.system", return_value="Linux"), \
             patch("app.services.dhcp_service._is_wsl", return_value=True):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_os()
        assert exc_info.value.reason == DhcpEnvReason.WSL_DETECTED
        assert "WSL" in exc_info.value.detail

    async def test_macos_rejected(self):
        with patch("platform.system", return_value="Darwin"):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_os()
        assert exc_info.value.reason == DhcpEnvReason.UNSUPPORTED_OS
        assert "macOS" in exc_info.value.detail

    async def test_unknown_os_rejected(self):
        with patch("platform.system", return_value="FreeBSD"):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_os()
        assert exc_info.value.reason == DhcpEnvReason.UNSUPPORTED_OS


# ---------------------------------------------------------------------------
# _check_powershell_binary — unit tests
# ---------------------------------------------------------------------------

class TestCheckPowershellBinary:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_passes_when_ps_found_and_runs(self):
        with patch("shutil.which", return_value="C:\\Windows\\powershell.exe"), \
             patch("app.services.dhcp_service._run_powershell_check", new=AsyncMock(return_value=(0, ""))):
            await _check_powershell_binary()  # must not raise

    async def test_rejected_when_powershell_not_on_path(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                await _check_powershell_binary()
        assert exc_info.value.reason == DhcpEnvReason.POWERSHELL_NOT_FOUND
        assert "powershell" in exc_info.value.detail.lower()

    async def test_rejected_when_powershell_exits_nonzero(self):
        with patch("shutil.which", return_value="C:\\powershell.exe"), \
             patch(
                 "app.services.dhcp_service._run_powershell_check",
                 new=AsyncMock(return_value=(1, "execution policy error")),
             ):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                await _check_powershell_binary()
        assert exc_info.value.reason == DhcpEnvReason.POWERSHELL_EXEC_FAILED
        assert "rc=1" in exc_info.value.detail


# ---------------------------------------------------------------------------
# _check_dhcp_cmdlets — unit tests
# ---------------------------------------------------------------------------

class TestCheckDhcpCmdlets:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_passes_when_get_command_succeeds(self):
        with patch("app.services.dhcp_service._run_powershell_check", new=AsyncMock(return_value=(0, ""))):
            await _check_dhcp_cmdlets()  # must not raise

    async def test_rejected_when_get_command_fails(self):
        with patch(
            "app.services.dhcp_service._run_powershell_check",
            new=AsyncMock(return_value=(1, "CommandNotFoundException")),
        ):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                await _check_dhcp_cmdlets()
        assert exc_info.value.reason == DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE
        assert "Get-DhcpServerv4Scope" in exc_info.value.detail

    async def test_uses_get_command_not_get_module(self):
        """Must use Get-Command (command availability), not Get-Module (module listing)."""
        captured_cmd: list[str] = []

        async def capture(command, _timeout):
            captured_cmd.append(command)
            return 0, ""

        with patch("app.services.dhcp_service._run_powershell_check", side_effect=capture):
            await _check_dhcp_cmdlets()

        ps_cmd = captured_cmd[0]
        assert "Get-Command" in ps_cmd
        assert "Get-DhcpServerv4Scope" in ps_cmd
        assert "Get-Module" not in ps_cmd
        assert "Get-WindowsFeature" not in ps_cmd


# ---------------------------------------------------------------------------
# validate_dhcp_environment — full flow + caching
# ---------------------------------------------------------------------------

class TestValidateDhcpEnvironment:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_passes_when_all_checks_succeed(self):
        with patch("app.services.dhcp_service._check_os"), \
             patch("app.services.dhcp_service._check_powershell_binary"), \
             patch("app.services.dhcp_service._check_dhcp_cmdlets"):
            await validate_dhcp_environment()  # must not raise

    async def test_raises_on_os_failure(self):
        exc = _env_error(DhcpEnvReason.WSL_DETECTED, "WSL not supported")
        with patch("app.services.dhcp_service._check_os", side_effect=exc):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                await validate_dhcp_environment()
        assert exc_info.value.reason == DhcpEnvReason.WSL_DETECTED

    async def test_raises_on_powershell_failure(self):
        exc = _env_error(DhcpEnvReason.POWERSHELL_NOT_FOUND, "not found")
        with patch("app.services.dhcp_service._check_os"), \
             patch("app.services.dhcp_service._check_powershell_binary", side_effect=exc):
            with pytest.raises(DhcpEnvironmentError):
                await validate_dhcp_environment()

    async def test_raises_on_cmdlet_failure(self):
        exc = _env_error(DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE, "no cmdlets")
        with patch("app.services.dhcp_service._check_os"), \
             patch("app.services.dhcp_service._check_powershell_binary"), \
             patch("app.services.dhcp_service._check_dhcp_cmdlets", side_effect=exc):
            with pytest.raises(DhcpEnvironmentError):
                await validate_dhcp_environment()

    async def test_result_cached_on_success(self):
        """Checks must only run once — second call must not invoke sub-checks."""
        check_os = patch("app.services.dhcp_service._check_os")
        check_ps = patch("app.services.dhcp_service._check_powershell_binary")
        check_cmd = patch("app.services.dhcp_service._check_dhcp_cmdlets")

        with check_os as m_os, check_ps as m_ps, check_cmd as m_cmd:
            await validate_dhcp_environment()
            await validate_dhcp_environment()  # second call

        assert m_os.call_count == 1
        assert m_ps.call_count == 1
        assert m_cmd.call_count == 1

    async def test_failure_cached(self):
        """A failed environment must fail all subsequent calls without re-running checks."""
        exc = _env_error(DhcpEnvReason.WSL_DETECTED, "WSL")
        check_os_mock = MagicMock(side_effect=exc)

        with patch("app.services.dhcp_service._check_os", check_os_mock):
            with pytest.raises(DhcpEnvironmentError):
                await validate_dhcp_environment()

        # Second call — _check_os should NOT be called again (cache hit)
        with pytest.raises(DhcpEnvironmentError):
            await validate_dhcp_environment()

        assert check_os_mock.call_count == 1  # called only once


# ---------------------------------------------------------------------------
# Route-level protection — DHCP routes return 503 for unsupported environments
# ---------------------------------------------------------------------------

class TestRouteLevelProtection:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    def _scope_path(self):
        return "/api/v1/scopes/10.20.30.0"

    async def test_dhcp_route_returns_503_on_wsl(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL not supported")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", self._scope_path())
        assert r.status_code == 503
        err = _error(r.json())
        assert err["code"] == "DHCP_ENVIRONMENT_UNAVAILABLE"
        assert err["details"]["reason"] == DhcpEnvReason.WSL_DETECTED

    async def test_dhcp_route_returns_503_on_linux(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.UNSUPPORTED_OS, "Linux not supported")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", self._scope_path())
        assert r.status_code == 503
        assert _error(r.json())["details"]["reason"] == DhcpEnvReason.UNSUPPORTED_OS

    async def test_dhcp_route_returns_503_on_no_powershell(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.POWERSHELL_NOT_FOUND, "PS not found")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", self._scope_path())
        assert r.status_code == 503
        assert _error(r.json())["details"]["reason"] == DhcpEnvReason.POWERSHELL_NOT_FOUND

    async def test_dhcp_route_returns_503_on_cmdlets_unavailable(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE, "no cmdlets")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", self._scope_path())
        assert r.status_code == 503
        assert _error(r.json())["details"]["reason"] == DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE

    async def test_post_route_also_protected(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL")
        payload = {
            "scopeName": "Test", "subnetMask": "255.255.255.0",
            "startRange": "10.20.30.100", "endRange": "10.20.30.200",
            "leaseDurationDays": 8, "description": "", "gateway": "10.20.30.1",
            "dnsServers": ["10.0.0.53"], "dnsDomain": "", "exclusions": [], "failover": None,
        }
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("POST", "/api/v1/scopes/10.20.30.0", json=payload)
        assert r.status_code == 503

    async def test_delete_route_also_protected(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("DELETE", self._scope_path())
        assert r.status_code == 503

    async def test_list_route_also_protected(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", "/api/v1/scopes")
        assert r.status_code == 503

    async def test_503_body_has_reason_and_detail(self):
        """Response must always have machine-readable code/reason and human-readable message."""
        exc = DhcpEnvironmentError(DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE, "cmdlet missing")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", self._scope_path())
        err = _error(r.json())
        assert err["code"] == "DHCP_ENVIRONMENT_UNAVAILABLE"
        assert err["message"] == "DHCP PowerShell cmdlets are unavailable"
        assert err["details"]["reason"] == DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Execution-layer guard — await run_ps() must also guard independently
# ---------------------------------------------------------------------------

class TestExecutionLayerGuard:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_run_ps_raises_dhcp_service_error_when_env_invalid(self):
        """await run_ps() must raise DhcpEnvironmentError even if no route dependency ran."""
        from app.services.ps_executor import run_ps

        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                await run_ps("Get-DhcpServerv4Scope -ScopeId 10.20.30.0")
        assert exc_info.value.reason == DhcpEnvReason.WSL_DETECTED

    async def test_run_ps_does_not_call_subprocess_in_bad_env(self):
        """Subprocess must never be invoked when env validation fails."""
        from app.services.ps_executor import run_ps

        exc = DhcpEnvironmentError(DhcpEnvReason.POWERSHELL_NOT_FOUND, "no PS")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc), \
             patch("asyncio.create_subprocess_exec") as mock_sub:
            with pytest.raises(DhcpEnvironmentError):
                await run_ps("Get-DhcpServerv4Scope")
        mock_sub.assert_not_called()


# ---------------------------------------------------------------------------
# Health endpoint — remains callable and reports env errors explicitly
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    pytestmark = pytest.mark.asyncio

    def setup_method(self):
        _reset_validation_cache()

    async def test_healthz_returns_200_when_env_valid(self):
        with patch("app.services.dhcp_service.validate_dhcp_environment"):
            r = await _request("GET", "/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_healthz_returns_503_with_reason_on_env_error(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE, "no cmdlets")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", "/healthz")
        assert r.status_code == 503
        err = _error(r.json())
        assert err["code"] == "DHCP_ENVIRONMENT_UNAVAILABLE"
        assert err["details"]["reason"] == DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE

    async def test_healthz_returns_503_for_wsl(self):
        exc = DhcpEnvironmentError(DhcpEnvReason.WSL_DETECTED, "WSL")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", "/healthz")
        assert r.status_code == 503
        assert _error(r.json())["details"]["reason"] == DhcpEnvReason.WSL_DETECTED

    async def test_healthz_callable_regardless_of_scope_router_protection(self):
        """Health endpoint must not inherit the scopes router dependency."""
        # Patch validate_dhcp_environment to fail — health endpoint must still respond
        exc = DhcpEnvironmentError(DhcpEnvReason.UNSUPPORTED_OS, "not Windows")
        with patch("app.services.dhcp_service.validate_dhcp_environment", side_effect=exc):
            r = await _request("GET", "/healthz")
        # Returns 503 (the handler runs and returns a structured response),
        # not a 500 unhandled exception
        assert r.status_code == 503
        assert _error(r.json())["details"]["reason"] == DhcpEnvReason.UNSUPPORTED_OS
