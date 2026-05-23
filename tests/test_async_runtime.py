import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.errors import PowerShellExecutionError, PowerShellTimeoutError
from app.services.ps_executor import run_ps
from app.utils.decorators import log_call
from app.utils.locks import ScopeLockManager

pytestmark = pytest.mark.asyncio


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        delay: float = 0,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.delay = delay
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


async def test_async_powershell_success_json_output():
    process = FakeProcess(stdout=b'{"Name":"Scope","ScopeId":"10.20.30.0"}')

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        result = await run_ps("Get-DhcpServerv4Scope -ScopeId 10.20.30.0")

    assert result == {"Name": "Scope", "ScopeId": "10.20.30.0"}


async def test_async_powershell_success_no_output():
    process = FakeProcess(stdout=b"")

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        result = await run_ps("Set-DhcpServerv4Scope -ScopeId 10.20.30.0", parse_json=False)

    assert result is None


async def test_async_powershell_nonzero_exit_raises_execution_error():
    process = FakeProcess(returncode=5, stderr=b"Access denied")

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        with pytest.raises(PowerShellExecutionError) as exc_info:
            await run_ps("Set-DhcpServerv4Scope -ScopeId 10.20.30.0", parse_json=False)

    assert exc_info.value.returncode == 5
    assert "Access denied" in exc_info.value.stderr


async def test_async_powershell_invalid_json_raises_execution_error():
    process = FakeProcess(stdout=b"not-json")

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        with pytest.raises(PowerShellExecutionError) as exc_info:
            await run_ps("Get-DhcpServerv4Scope")

    assert exc_info.value.returncode == 0
    assert "non-JSON" in exc_info.value.stderr


async def test_async_powershell_timeout_kills_and_waits(monkeypatch):
    process = FakeProcess(returncode=None, delay=1)
    monkeypatch.setattr(settings, "POWERSHELL_COMMAND_TIMEOUT_SECONDS", 0.01)

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        with pytest.raises(PowerShellTimeoutError):
            await run_ps("Get-DhcpServerv4Scope")

    assert process.killed is True
    assert process.waited is True


async def test_global_powershell_concurrency_limit(monkeypatch):
    monkeypatch.setattr(settings, "POWERSHELL_MAX_CONCURRENCY", 2)
    active = 0
    max_active = 0
    active_lock = asyncio.Lock()

    class CountingProcess(FakeProcess):
        async def communicate(self):
            nonlocal active, max_active
            async with active_lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            async with active_lock:
                active -= 1
            return b"", b""

    async def create_process(*_args, **_kwargs):
        return CountingProcess()

    with patch("app.services.dhcp_service.validate_dhcp_environment"), \
         patch("asyncio.create_subprocess_exec", side_effect=create_process):
        await asyncio.gather(
            *(run_ps(f"Get-DhcpServerv4Scope -ScopeId 10.20.30.{i}", parse_json=False) for i in range(6))
        )

    assert max_active == 2


async def test_same_scope_lock_serializes_operations():
    locks = ScopeLockManager()
    active = 0
    max_active = 0

    async def worker():
        nonlocal active, max_active
        async with locks.lock("10.20.30.0"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(worker(), worker(), worker())

    assert max_active == 1


async def test_different_scope_locks_allow_parallel_operations():
    locks = ScopeLockManager()
    active = 0
    max_active = 0

    async def worker(scope_id: str):
        nonlocal active, max_active
        async with locks.lock(scope_id):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(worker("10.20.30.0"), worker("10.20.40.0"))

    assert max_active == 2


async def test_log_call_preserves_and_awaits_async_function():
    @log_call
    async def sample(value: int) -> int:
        await asyncio.sleep(0)
        return value + 1

    assert inspect.iscoroutinefunction(sample)
    assert await sample(41) == 42
