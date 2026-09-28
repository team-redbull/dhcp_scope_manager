"""Tests for the log_call decorator and ScopeLockManager.

test_async_runtime.py covers async lock serialisation and log_call with
an async function.  This file adds:
- log_call with a sync function
- log_call preserves function metadata (__name__, __doc__, __module__)
- log_call does not swallow exceptions (sync and async)
- ScopeLockManager: same scope reuses the same lock object
- ScopeLockManager: lock is released after an exception
- ScopeLockManager: lock is released after normal exit
"""
import asyncio
import inspect
import pytest
from app.utils.decorators import log_call
from app.utils.locks import ScopeLockManager


# ─── log_call decorator ───────────────────────────────────────────────────────

class TestLogCallDecorator:

    def test_sync_function_returns_correct_value(self):
        @log_call
        def add(a: int, b: int) -> int:
            return a + b

        assert add(3, 4) == 7

    def test_sync_function_is_not_coroutine(self):
        @log_call
        def sync_fn():
            return 1

        result = sync_fn()
        assert not inspect.iscoroutine(result)

    def test_async_function_remains_awaitable(self):
        @log_call
        async def async_fn():
            return 42

        assert inspect.iscoroutinefunction(async_fn)

    @pytest.mark.asyncio
    async def test_async_function_returns_correct_value(self):
        @log_call
        async def double(x: int) -> int:
            await asyncio.sleep(0)
            return x * 2

        assert await double(21) == 42

    def test_preserves_function_name(self):
        @log_call
        def my_special_function():
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_preserves_docstring(self):
        @log_call
        def documented():
            """This is the docstring."""
            pass

        assert documented.__doc__ == "This is the docstring."

    def test_preserves_module(self):
        @log_call
        def some_fn():
            pass

        assert some_fn.__module__ == __name__

    def test_sync_does_not_swallow_exceptions(self):
        @log_call
        def explode():
            raise ValueError("sync boom")

        with pytest.raises(ValueError, match="sync boom"):
            explode()

    @pytest.mark.asyncio
    async def test_async_does_not_swallow_exceptions(self):
        @log_call
        async def async_explode():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            await async_explode()

    def test_sync_logs_entry_and_exit(self, caplog):
        import logging

        @log_call
        def traced():
            return "ok"

        with caplog.at_level(logging.INFO):
            traced()

        assert "traced" in caplog.text

    @pytest.mark.asyncio
    async def test_async_logs_entry_and_exit(self, caplog):
        import logging

        @log_call
        async def async_traced():
            return "ok"

        with caplog.at_level(logging.INFO):
            await async_traced()

        assert "async_traced" in caplog.text

    def test_sync_logs_on_exception(self, caplog):
        import logging

        @log_call
        def failing_fn():
            raise ValueError("oops")

        with caplog.at_level(logging.INFO):
            try:
                failing_fn()
            except ValueError:
                pass

        assert "failing_fn" in caplog.text

    @pytest.mark.asyncio
    async def test_async_logs_on_exception(self, caplog):
        import logging

        @log_call
        async def async_failing():
            raise ValueError("async oops")

        with caplog.at_level(logging.INFO):
            try:
                await async_failing()
            except ValueError:
                pass

        assert "async_failing" in caplog.text

    def test_sync_passthrough_args_and_kwargs(self):
        @log_call
        def sum_all(*args, mult=1):
            return sum(args) * mult

        assert sum_all(1, 2, 3, mult=2) == 12

    @pytest.mark.asyncio
    async def test_async_passthrough_args_and_kwargs(self):
        @log_call
        async def concat(a, b, sep=""):
            return f"{a}{sep}{b}"

        assert await concat("hello", "world", sep="-") == "hello-world"

    # ── structured extra fields ───────────────────────────────────────────────

    def test_sync_emits_operation_field(self, caplog):
        import logging

        @log_call
        def my_op():
            pass

        with caplog.at_level(logging.INFO):
            my_op()

        records = [r for r in caplog.records if "my_op" in r.getMessage()]
        assert records, "expected at least one log record mentioning my_op"
        for rec in records:
            assert getattr(rec, "operation", None) == "my_op"

    @pytest.mark.asyncio
    async def test_async_emits_operation_field(self, caplog):
        import logging

        @log_call
        async def async_my_op():
            pass

        with caplog.at_level(logging.INFO):
            await async_my_op()

        records = [r for r in caplog.records if "async_my_op" in r.getMessage()]
        assert records
        for rec in records:
            assert getattr(rec, "operation", None) == "async_my_op"

    def test_exit_record_has_duration_ms_and_status_ok(self, caplog):
        import logging

        @log_call
        def quick():
            return 1

        with caplog.at_level(logging.INFO):
            quick()

        exit_rec = next(
            (r for r in caplog.records if "←" in r.getMessage() and "quick" in r.getMessage()),
            None,
        )
        assert exit_rec is not None, "no exit log record found"
        assert getattr(exit_rec, "status", None) == "ok"
        assert isinstance(getattr(exit_rec, "duration_ms", None), float)

    @pytest.mark.asyncio
    async def test_async_exit_record_has_duration_ms_and_status_ok(self, caplog):
        import logging

        @log_call
        async def aquick():
            return 1

        with caplog.at_level(logging.INFO):
            await aquick()

        exit_rec = next(
            (r for r in caplog.records if "←" in r.getMessage() and "aquick" in r.getMessage()),
            None,
        )
        assert exit_rec is not None
        assert getattr(exit_rec, "status", None) == "ok"
        assert isinstance(getattr(exit_rec, "duration_ms", None), float)

    def test_error_exit_has_status_error(self, caplog):
        import logging

        @log_call
        def boom():
            raise RuntimeError("x")

        with caplog.at_level(logging.INFO):
            try:
                boom()
            except RuntimeError:
                pass

        exit_rec = next(
            (r for r in caplog.records if "raised" in r.getMessage()),
            None,
        )
        assert exit_rec is not None
        assert getattr(exit_rec, "status", None) == "error"

    def test_scope_id_extracted_from_positional_arg(self, caplog):
        import logging

        @log_call
        def with_scope(scope: str, other: int = 0) -> str:
            return scope

        with caplog.at_level(logging.INFO):
            with_scope("10.20.30.0", other=5)

        records = [r for r in caplog.records if "with_scope" in r.getMessage()]
        assert records
        for rec in records:
            assert getattr(rec, "scope", None) == "10.20.30.0"

    @pytest.mark.asyncio
    async def test_async_scope_id_extracted_from_kwarg(self, caplog):
        import logging

        @log_call
        async def async_with_scope(scope: str) -> str:
            return scope

        with caplog.at_level(logging.INFO):
            await async_with_scope(scope="10.20.30.1")

        records = [r for r in caplog.records if "async_with_scope" in r.getMessage()]
        assert records
        for rec in records:
            assert getattr(rec, "scope", None) == "10.20.30.1"

    def test_no_scope_id_param_omits_field(self, caplog):
        import logging

        @log_call
        def no_scope(x: int) -> int:
            return x

        with caplog.at_level(logging.INFO):
            no_scope(42)

        records = [r for r in caplog.records if "no_scope" in r.getMessage()]
        assert records
        for rec in records:
            assert not hasattr(rec, "scope") or getattr(rec, "scope", None) is None


# ─── ScopeLockManager ─────────────────────────────────────────────────────────

class TestScopeLockManager:
    pytestmark = pytest.mark.asyncio

    async def test_same_scope_id_reuses_same_lock(self):
        """Two lock() calls for the same scope must return the same asyncio.Lock object."""
        manager = ScopeLockManager()
        lock_a = await manager._get_lock("10.20.30.0")
        lock_b = await manager._get_lock("10.20.30.0")
        assert lock_a is lock_b

    async def test_different_scope_ids_use_different_locks(self):
        manager = ScopeLockManager()
        lock_a = await manager._get_lock("10.20.30.0")
        lock_b = await manager._get_lock("10.20.40.0")
        assert lock_a is not lock_b

    async def test_lock_released_after_normal_exit(self):
        """After the async context manager exits normally, the lock must be free."""
        manager = ScopeLockManager()
        async with manager.lock("10.20.30.0"):
            pass

        lock = await manager._get_lock("10.20.30.0")
        assert not lock.locked()

    async def test_lock_released_after_exception_inside_context(self):
        """Even when the body raises, the lock must be released."""
        manager = ScopeLockManager()
        with pytest.raises(ValueError):
            async with manager.lock("10.20.30.0"):
                raise ValueError("error inside lock")

        lock = await manager._get_lock("10.20.30.0")
        assert not lock.locked()

    async def test_second_acquire_blocked_while_first_holds(self):
        """While one coroutine holds the lock, another must wait."""
        manager = ScopeLockManager()
        acquired_count = 0
        max_concurrent = 0

        async def worker():
            nonlocal acquired_count, max_concurrent
            async with manager.lock("10.20.30.0"):
                acquired_count += 1
                max_concurrent = max(max_concurrent, acquired_count)
                await asyncio.sleep(0.01)
                acquired_count -= 1

        await asyncio.gather(worker(), worker(), worker())
        assert max_concurrent == 1

    async def test_different_scopes_can_run_concurrently(self):
        """Locks for different scopes must not block each other."""
        manager = ScopeLockManager()
        acquired_count = 0
        max_concurrent = 0

        async def worker(scope: str):
            nonlocal acquired_count, max_concurrent
            async with manager.lock(scope):
                acquired_count += 1
                max_concurrent = max(max_concurrent, acquired_count)
                await asyncio.sleep(0.01)
                acquired_count -= 1

        await asyncio.gather(
            worker("10.20.30.0"),
            worker("10.20.40.0"),
            worker("10.20.50.0"),
        )
        assert max_concurrent > 1  # must have run in parallel

    async def test_lock_context_manager_is_reentrant_across_tasks(self):
        """Two tasks competing for the same lock must each run serially."""
        manager = ScopeLockManager()
        results = []

        async def task(name: str):
            async with manager.lock("shared"):
                results.append(f"enter-{name}")
                await asyncio.sleep(0.01)
                results.append(f"exit-{name}")

        await asyncio.gather(task("A"), task("B"))

        # Each enter must be immediately followed by its own exit
        assert results.index("exit-A") == results.index("enter-A") + 1 or \
               results.index("exit-B") == results.index("enter-B") + 1

    async def test_cancellation_releases_lock(self):
        """A cancelled coroutine holding the lock must release it so others can proceed."""
        manager = ScopeLockManager()
        lock_held = asyncio.Event()
        proceed = asyncio.Event()

        async def holder():
            async with manager.lock("10.20.30.0"):
                lock_held.set()
                await proceed.wait()  # will be cancelled here

        async def waiter():
            async with manager.lock("10.20.30.0"):
                return "acquired"

        holder_task = asyncio.create_task(holder())
        await lock_held.wait()
        holder_task.cancel()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass

        # Lock must be released — waiter should be able to acquire it
        result = await asyncio.wait_for(waiter(), timeout=1.0)
        assert result == "acquired"
