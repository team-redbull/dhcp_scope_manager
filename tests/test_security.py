"""Security-focused tests.

Covers:
- PowerShell string escaping (ps_single_quote)
- scope injection prevention
- API response sanitization: no Windows paths, no raw stderr, no stack traces
- Secret values not in log output or exception messages
- Exception handler helper unit tests (_sanitize_text, _is_already_exists_error)
"""
import logging
import json
import pytest
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.errors import InvalidScopeError
from app.exception_handlers import _sanitize_text
from app.services.ps_executor import is_already_exists_error as _is_already_exists_error
from app.logging_config import _SafeJsonFormatter
from app.errors import PowerShellError, PowerShellTimeoutError
from app.services.ps_parsers import build_get_scope_state_script, ps_single_quote


def _scope_body(**overrides):
    base = dict(
        scopeName="Test", subnetMask="255.255.255.0",
        startRange="10.20.30.100", endRange="10.20.30.200",
        leaseDurationDays=8, description="", gateway="10.20.30.1",
        dnsServers=["10.0.0.53"], dnsDomain="", exclusions=[], failover=None,
    )
    base.update(overrides)
    return base


async def _post(json_body):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/api/v1/scopes/10.20.30.0", json=json_body)


# ─── ps_single_quote escaping ─────────────────────────────────────────────────

class TestPsSingleQuoteEscaping:

    def test_wraps_in_single_quotes(self):
        assert ps_single_quote("10.20.30.0") == "'10.20.30.0'"

    def test_doubles_embedded_single_quote(self):
        assert ps_single_quote("O'Brien") == "'O''Brien'"

    def test_multiple_single_quotes(self):
        assert ps_single_quote("a'b'c") == "'a''b''c'"

    def test_empty_string(self):
        assert ps_single_quote("") == "''"

    def test_classic_injection_neutralized(self):
        """'; Remove-DhcpServerv4Scope' injection breaks when single-quote is doubled."""
        malicious = "'; Remove-DhcpServerv4Scope -ScopeId 1.1.1.0; '"
        escaped = ps_single_quote(malicious)
        assert escaped.startswith("'")
        assert escaped.endswith("'")
        # All single quotes doubled
        inner = escaped[1:-1]
        assert "''" in inner  # the quote was doubled, breaking the injection


# ─── scope injection ───────────────────────────────────────────────────────

class TestScopeIdInjection:

    def test_semicolon_injection_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("10.20.30.0; Invoke-Expression 'evil'")

    def test_single_quote_injection_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("10.20.30.0'; Remove-DhcpServerv4Scope -Force; '")

    def test_pipe_injection_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("10.20.30.0 | Remove-Item C:\\ -Recurse")

    def test_newline_injection_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("10.20.30.0\nRemove-DhcpServerv4Scope")

    def test_dollar_variable_injection_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("$ScopeId")

    def test_hostname_as_scope_id_rejected(self):
        with pytest.raises(InvalidScopeError):
            build_get_scope_state_script("dhcp-server.lab.local")

    def test_valid_scope_id_accepted(self):
        script = build_get_scope_state_script("10.20.30.0")
        assert "10.20.30.0" in script

    def test_valid_scope_id_single_quoted_in_script(self):
        """scope must be single-quoted in the generated PS script."""
        script = build_get_scope_state_script("10.20.30.0")
        assert "$ScopeId = '10.20.30.0'" in script


# ─── _sanitize_text ──────────────────────────────────────────────────────────

class TestSanitizeText:

    def test_strips_windows_absolute_path(self):
        result = _sanitize_text("Error at C:\\Windows\\system32\\cmd.exe line 5")
        assert "C:\\" not in result
        assert "<path>" in result

    def test_strips_path_with_subdirectory(self):
        result = _sanitize_text("Failed: C:\\Users\\Admin\\scripts\\deploy.ps1")
        assert "C:\\" not in result

    def test_strips_d_drive_path(self):
        result = _sanitize_text("Module at D:\\PowerShell\\DHCPServer.psm1")
        assert "D:\\" not in result

    def test_truncates_at_500_chars(self):
        long_text = "a" * 600
        assert len(_sanitize_text(long_text)) == 500

    def test_truncates_at_custom_max_len(self):
        assert _sanitize_text("abcde", max_len=3) == "abc"

    def test_plain_text_unchanged(self):
        text = "Access denied to DHCP server"
        assert _sanitize_text(text) == text

    def test_empty_string_unchanged(self):
        assert _sanitize_text("") == ""


# ─── _is_already_exists_error ────────────────────────────────────────────────

class TestIsAlreadyExistsError:

    def test_already_exists_keyword(self):
        assert _is_already_exists_error("The relationship already exists")

    def test_already_been_added_keyword(self):
        assert _is_already_exists_error("The scope has already been added to this relationship")

    def test_already_in_use_keyword(self):
        assert _is_already_exists_error("The IP address is already in use")

    def test_case_insensitive(self):
        assert _is_already_exists_error("ALREADY EXISTS")
        assert _is_already_exists_error("Already In Use")

    def test_access_denied_false(self):
        assert not _is_already_exists_error("Access denied")

    def test_not_found_false(self):
        assert not _is_already_exists_error("Object not found")

    def test_empty_false(self):
        assert not _is_already_exists_error("")


# ─── API response: no infrastructure details leaked ──────────────────────────

class TestApiResponseSanitization:
    pytestmark = pytest.mark.asyncio

    async def test_windows_path_stripped_from_500_response(self):
        with patch(
            "app.services.scope_service.create_scope",
            side_effect=PowerShellError(
                "Add-DhcpServerv4Scope",
                "Error at C:\\Windows\\system32\\dhcpcore.dll line 42",
                1,
            ),
        ):
            r = await _post(_scope_body())
        assert r.status_code == 500
        assert "C:\\" not in r.text

    async def test_raw_ps_command_not_in_500_response(self):
        with patch(
            "app.services.scope_service.create_scope",
            side_effect=PowerShellError("Add-DhcpServerv4Scope", "Access denied", 1),
        ):
            r = await _post(_scope_body())
        assert r.status_code == 500
        assert "Add-DhcpServerv4Scope" not in r.text

    async def test_no_stack_trace_in_500_response(self):
        with patch("app.services.scope_service.list_scopes", side_effect=RuntimeError("boom")):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                r = await client.get("/api/v1/scopes")
        assert r.status_code == 500
        assert "Traceback" not in r.text
        assert "RuntimeError" not in r.text
        assert "boom" not in r.text

    async def test_timeout_response_does_not_expose_command(self):
        with patch(
            "app.services.scope_service.create_scope",
            side_effect=PowerShellTimeoutError("Add-DhcpServerv4Scope -Name secret-scope", 60),
        ):
            r = await _post(_scope_body())
        assert r.status_code == 504
        assert "Add-DhcpServerv4Scope" not in r.text

    async def test_500_response_has_standard_shape(self):
        with patch(
            "app.services.scope_service.create_scope",
            side_effect=PowerShellError("cmd", "err", 1),
        ):
            r = await _post(_scope_body())
        assert r.status_code == 500
        data = r.json()
        assert "error" in data
        err = data["error"]
        assert set(err.keys()) >= {"code", "message", "details"}

    async def test_404_response_has_standard_shape(self):
        from app.errors import ScopeNotFoundError
        with patch(
            "app.services.scope_service.get_scope",
            side_effect=ScopeNotFoundError("10.20.30.0"),
        ):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                r = await client.get("/api/v1/scopes/10.20.30.0")
        assert r.status_code == 404
        data = r.json()
        assert "error" in data
        assert data["error"]["code"] == "SCOPE_NOT_FOUND"

    async def test_raw_stderr_not_in_response_body(self):
        """Sensitive PowerShell stderr (e.g. server names, credentials) must not be in response."""
        sensitive_stderr = "secret-server.internal.corp Access denied for user admin"
        with patch(
            "app.services.scope_service.create_scope",
            side_effect=PowerShellError("cmd", sensitive_stderr, 5),
        ):
            r = await _post(_scope_body())
        assert r.status_code == 500
        assert "secret-server.internal.corp" not in r.text

# ─── PowerShellError safe string / JSON logging ──────────────────────────────

class TestPowerShellErrorSafety:

    def test_powershell_error_str_redacts_windows_path(self):
        err = PowerShellError(
            "Set-DhcpServerv4Failover",
            "Failure in C:\\Windows\\System32\\dhcp.dll",
            1,
            operation="set_failover_params",
            scope="10.20.30.0",
        )
        text = str(err)
        assert "C:\\" not in text
        assert "<path>" in text

    def test_json_formatter_includes_safe_extra_fields(self):
        formatter = _SafeJsonFormatter()
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "message %s", ("ok",), None
        )
        record.scope = "10.20.30.0"
        record.operation = "set_dns_options"
        record.relationship_name = "rel1"
        record.duration_ms = 12.5
        record.status = "ok"
        record.error_code = "POWERSHELL_COMMAND_FAILED"
        rendered = formatter.format(record)
        payload = json.loads(rendered)
        assert payload["scope"] == "10.20.30.0"
        assert payload["operation"] == "set_dns_options"
        assert payload["relationship_name"] == "rel1"
        assert payload["duration_ms"] == 12.5
        assert payload["status"] == "ok"
        assert payload["error_code"] == "POWERSHELL_COMMAND_FAILED"

    def test_json_formatter_handles_absent_scope(self):
        formatter = _SafeJsonFormatter()
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hello", (), None)
        payload = json.loads(formatter.format(record))
        assert payload["msg"] == "hello"
        assert "scope" not in payload
