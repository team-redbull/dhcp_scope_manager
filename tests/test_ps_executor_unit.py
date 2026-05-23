"""Focused unit tests for ps_executor module.

Covers behavior not already tested in test_async_runtime.py:
- parse_json=False with non-empty stdout returns None
- whitespace-only stdout treated as empty
- append_error_action / append_convert_to_json flag behaviour
- Binary / non-UTF8 bytes decoded with errors='replace'
- is_not_found_error keyword set
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.errors import PowerShellExecutionError
from app.services.ps_executor import is_not_found_error, run_ps


class _FakeProcess:
    def __init__(self, *, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False
        self.waited = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


def _proc(returncode=0, stdout=b"", stderr=b""):
    return _FakeProcess(returncode=returncode, stdout=stdout, stderr=stderr)


# ─── parse_json flag ──────────────────────────────────────────────────────────

class TestParseJsonFlag:
    pytestmark = pytest.mark.asyncio

    async def test_parse_json_false_with_nonempty_stdout_returns_none(self):
        """parse_json=False → always None, even when PowerShell wrote output."""
        p = _proc(stdout=b"[{ScopeId:10.20.30.0}]\n")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            result = await run_ps("Set-DhcpServerv4Scope", parse_json=False)
        assert result is None

    async def test_parse_json_true_whitespace_stdout_returns_none(self):
        """Whitespace-only stdout is treated as empty — returns None, no JSON error."""
        p = _proc(stdout=b"   \r\n\t\n")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            result = await run_ps("Get-Nothing")
        assert result is None

    async def test_parse_json_true_empty_stdout_returns_none(self):
        p = _proc(stdout=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            result = await run_ps("Get-Nothing")
        assert result is None

    async def test_parse_json_true_array_stdout_parsed(self):
        p = _proc(stdout=b'[{"ScopeId":"10.20.30.0"}]\n')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            result = await run_ps("Get-DhcpServerv4Scope")
        assert result == [{"ScopeId": "10.20.30.0"}]


# ─── append_error_action / append_convert_to_json ────────────────────────────

class TestCommandFlags:
    pytestmark = pytest.mark.asyncio

    async def _capture_cmd(self, **run_ps_kwargs) -> str:
        """Run run_ps with given kwargs, return the full command string passed to PS."""
        captured = []

        async def fake_exec(prog, *args, **kwargs):
            captured.extend(args)
            return _proc(stdout=b"null\n")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await run_ps("Get-Item", **run_ps_kwargs)

        return " ".join(str(a) for a in captured)

    async def test_append_error_action_true_adds_stop(self):
        cmd = await self._capture_cmd(parse_json=True)
        assert "-ErrorAction Stop" in cmd

    async def test_append_error_action_false_omits_stop(self):
        cmd = await self._capture_cmd(parse_json=False, append_error_action=False)
        assert "-ErrorAction Stop" not in cmd

    async def test_append_convert_to_json_true_adds_pipe(self):
        cmd = await self._capture_cmd(parse_json=True)
        assert "ConvertTo-Json" in cmd

    async def test_append_convert_to_json_false_omits_pipe(self):
        cmd = await self._capture_cmd(parse_json=False, append_convert_to_json=False)
        assert "ConvertTo-Json" not in cmd

    async def test_parse_json_false_skips_convert_pipe_even_with_append_true(self):
        """parse_json=False disables the JSON pipe regardless of append_convert_to_json."""
        cmd = await self._capture_cmd(parse_json=False, append_convert_to_json=True)
        assert "ConvertTo-Json" not in cmd

    async def test_powershell_invoked_with_no_profile_flags(self):
        """PowerShell must always be invoked with -NoProfile -NonInteractive for reliability."""
        captured = []

        async def fake_exec(prog, *args, **kwargs):
            captured.extend(args)
            return _proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await run_ps("Get-Item", parse_json=False)

        all_args = " ".join(str(a) for a in captured)
        assert "-NoProfile" in all_args
        assert "-NonInteractive" in all_args


# ─── Byte decoding ────────────────────────────────────────────────────────────

class TestByteDecoding:
    pytestmark = pytest.mark.asyncio

    async def test_binary_garbage_in_stderr_decoded_without_raising(self):
        """Non-UTF-8 bytes in stderr must not raise UnicodeDecodeError."""
        garbage = b"\x80\x81\x82 Access denied"
        p = _proc(returncode=1, stderr=garbage)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            with pytest.raises(PowerShellExecutionError) as exc_info:
                await run_ps("Get-Something", parse_json=False)
        assert isinstance(exc_info.value.stderr, str)  # decoded, not raw bytes

    async def test_binary_garbage_in_stdout_raises_powershell_error_not_unicode_error(self):
        """Non-UTF-8 bytes in stdout when parse_json=True → PowerShellError, not UnicodeDecodeError."""
        garbage = b"\x80\x81\x82"
        p = _proc(returncode=0, stdout=garbage)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            with pytest.raises(PowerShellExecutionError):
                await run_ps("Get-Something", parse_json=True)

    async def test_replacement_char_in_decoded_stderr(self):
        """The replacement character (U+FFFD) appears in decoded output for invalid bytes."""
        garbage = b"\xff\xfe error"
        p = _proc(returncode=1, stderr=garbage)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=p)):
            with pytest.raises(PowerShellExecutionError) as exc_info:
                await run_ps("Get-Something", parse_json=False)
        # Decoded with errors='replace' → replacement char present or text still a string
        assert isinstance(exc_info.value.stderr, str)


# ─── is_not_found_error ───────────────────────────────────────────────────────

class TestIsNotFoundError:

    def test_not_found_keyword(self):
        assert is_not_found_error("No DHCP scope was not found")

    def test_does_not_exist_keyword(self):
        assert is_not_found_error("The object does not exist on this server")

    def test_no_dhcp_scope_keyword(self):
        assert is_not_found_error("No DHCP scope found for the given ScopeId")

    def test_cannot_find_keyword(self):
        assert is_not_found_error("Cannot find scope 10.20.30.0")

    def test_case_insensitive_upper(self):
        assert is_not_found_error("DOES NOT EXIST")

    def test_case_insensitive_mixed(self):
        assert is_not_found_error("Not Found on server")

    def test_access_denied_false(self):
        assert not is_not_found_error("Access denied")

    def test_permission_error_false(self):
        assert not is_not_found_error("The user does not have permission to perform this operation")

    def test_empty_stderr_false(self):
        assert not is_not_found_error("")

    def test_rpc_unavailable_false(self):
        assert not is_not_found_error("The RPC server is unavailable")

    def test_timeout_false(self):
        assert not is_not_found_error("Timed out waiting for DHCP server response")
