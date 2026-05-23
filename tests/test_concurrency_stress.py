"""Concurrency and stress tests — no real DHCP server required.

Simulates Crossplane-like workloads:
- 100 concurrent GETs for different scopes (all succeed)
- Same-scope concurrent PUTs are serialized by ScopeLockManager
- Different-scope PUTs run in parallel
- PS semaphore limit is respected when N > POWERSHELL_MAX_CONCURRENCY
- Mixed GET/POST/PUT/DELETE for many scopes
- Error isolation: one failure does not affect other scopes
- No deadlocks, no un-awaited coroutines, no pending tasks

IMPORTANT: Tests that make concurrent HTTP calls must use a SINGLE patch(...)
context manager shared across all requests in the gather().  Using a per-coroutine
`with patch(...)` inside asyncio.gather() is NOT safe: if coroutine A enters first
and exits first (before B exits), it restores the attribute to the original, then
B exits and restores to what it saw on entry (A's mock) — leaving the attribute
pointing at a stale Mock object.
"""
import asyncio
import random
import time
from unittest.mock import patch

import pytest

from app.errors import ScopeNotFoundError
from app.models import DhcpScopePayload
from app.errors import PowerShellExecutionError, PowerShellTimeoutError
from app.utils.locks import ScopeLockManager
from httpx import ASGITransport, AsyncClient
from app.main import app

pytestmark = pytest.mark.asyncio


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _network(i: int) -> str:
    """Generate a unique /24 network for test scope i (i in 0..511)."""
    return f"10.{(i // 256) % 256}.{i % 256}.0"


def _make_scope(network: str = "10.20.30.0") -> DhcpScopePayload:
    prefix = ".".join(network.split(".")[:3]) + "."
    return DhcpScopePayload(
        scopeName="Scope",
        network=network,
        subnetMask="255.255.255.0",
        startRange=f"{prefix}100",
        endRange=f"{prefix}200",
        leaseDurationDays=8,
        description="",
        gateway=f"{prefix}1",
        dnsServers=["10.0.0.53"],
        dnsDomain="lab.local",
        exclusions=[],
        failover=None,
    )


def _scope_body(network: str, name: str = "Scope") -> dict:
    prefix = ".".join(network.split(".")[:3]) + "."
    return dict(
        scopeName=name,
        network=network,
        subnetMask="255.255.255.0",
        startRange=f"{prefix}100",
        endRange=f"{prefix}200",
        leaseDurationDays=8,
        description="",
        gateway=f"{prefix}1",
        dnsServers=["10.0.0.53"],
        dnsDomain="lab.local",
        exclusions=[],
        failover=None,
    )


# ─── 100 concurrent GETs for distinct scopes ─────────────────────────────────

class TestMassiveConcurrentGet:

    async def test_100_distinct_scope_gets_all_succeed(self):
        """100 concurrent GET /api/v1/scopes/{network} — all must return 200."""
        networks = [_network(i) for i in range(100)]

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                results = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in networks)
                )

        non_200 = [r.status_code for r in results if r.status_code != 200]
        assert not non_200, f"Non-200 statuses: {non_200}"

    async def test_150_distinct_scope_gets_all_succeed(self):
        """150 concurrent GETs stress-test the async machinery."""
        networks = [_network(i) for i in range(150)]

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                results = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in networks)
                )

        assert all(r.status_code == 200 for r in results)


# ─── Same-scope PUT serialization ─────────────────────────────────────────────

class TestSameScopeSerialization:

    async def test_same_scope_concurrent_puts_are_serialized(self):
        """N concurrent PUTs for the same network must not overlap: max_concurrent == 1."""
        manager = ScopeLockManager()
        acquired_count = 0
        max_concurrent = 0

        async def simulate_put(i: int):
            nonlocal acquired_count, max_concurrent
            async with manager.lock("10.20.30.0"):
                acquired_count += 1
                max_concurrent = max(max_concurrent, acquired_count)
                await asyncio.sleep(0.005)
                acquired_count -= 1

        await asyncio.gather(*(simulate_put(i) for i in range(20)))

        assert max_concurrent == 1, f"max_concurrent={max_concurrent} — lock not serializing"

    async def test_same_scope_entry_exit_interleaving_is_serial(self):
        """Each enter-N must be followed by exit-N before the next enter."""
        manager = ScopeLockManager()
        log = []

        async def worker(i: int):
            async with manager.lock("shared"):
                log.append(("enter", i))
                await asyncio.sleep(0.002)
                log.append(("exit", i))

        await asyncio.gather(*(worker(i) for i in range(10)))

        for pos in range(0, len(log), 2):
            action1, id1 = log[pos]
            action2, id2 = log[pos + 1]
            assert action1 == "enter" and action2 == "exit" and id1 == id2, (
                f"Serial violation at positions {pos}, {pos+1}: {log[pos:pos+2]}"
            )


# ─── Different-scope PUTs run in parallel ─────────────────────────────────────

class TestDifferentScopeParallelism:

    async def test_different_scopes_do_not_block_each_other(self):
        """30 different scopes must run concurrently — wall clock < serial bound."""
        manager = ScopeLockManager()
        acquired_count = 0
        max_concurrent = 0
        SCOPE_COUNT = 30

        async def worker(scope_id: str):
            nonlocal acquired_count, max_concurrent
            async with manager.lock(scope_id):
                acquired_count += 1
                max_concurrent = max(max_concurrent, acquired_count)
                await asyncio.sleep(0.01)
                acquired_count -= 1

        scopes = [_network(i) for i in range(SCOPE_COUNT)]
        t0 = time.monotonic()
        await asyncio.gather(*(worker(s) for s in scopes))
        elapsed = time.monotonic() - t0

        assert max_concurrent > 1, "Different scopes did not run in parallel"
        # Serial: 30 * 0.01 = 0.3s; parallel should be well under
        assert elapsed < 0.2, f"Likely not running in parallel: {elapsed:.3f}s for {SCOPE_COUNT} scopes"

    async def test_100_scopes_parallel_wall_clock(self):
        """100 scopes each sleeping 0.01s must complete in well under 1s."""
        manager = ScopeLockManager()

        async def worker(scope_id: str):
            async with manager.lock(scope_id):
                await asyncio.sleep(0.01)

        scopes = [_network(i) for i in range(100)]
        t0 = time.monotonic()
        await asyncio.gather(*(worker(s) for s in scopes))
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"100 distinct scopes not parallel: {elapsed:.3f}s"


# ─── Semaphore limit enforcement ──────────────────────────────────────────────

class TestSemaphoreLimitEnforcement:

    async def test_semaphore_limits_concurrent_ps_calls(self):
        """At most POWERSHELL_MAX_CONCURRENCY run_ps calls should be active simultaneously."""
        from app.services import ps_executor
        import app.config as cfg

        limit = 5
        concurrent_active = 0
        max_seen = 0

        async def fake_exec(*args, **kwargs):
            nonlocal concurrent_active, max_seen
            concurrent_active += 1
            max_seen = max(max_seen, concurrent_active)
            await asyncio.sleep(0.02)
            concurrent_active -= 1

            class FakeProc:
                returncode = 0
                async def communicate(self):
                    return b"null\n", b""
            return FakeProc()

        with patch.object(cfg.settings, "POWERSHELL_MAX_CONCURRENCY", limit):
            ps_executor._semaphore = None
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                from app.services.ps_executor import run_ps
                tasks = [run_ps("Get-Item", parse_json=False) for _ in range(limit * 4)]
                await asyncio.gather(*tasks)

        assert max_seen <= limit, f"Semaphore exceeded: max_seen={max_seen} > limit={limit}"
        ps_executor._semaphore = None

    async def test_semaphore_does_not_deadlock_at_limit(self):
        """N tasks waiting on a semaphore must all complete without deadlock."""
        from app.services import ps_executor

        N = 8

        async def fake_exec(*args, **kwargs):
            class FakeProc:
                returncode = 0
                async def communicate(self):
                    return b"null\n", b""
            return FakeProc()

        ps_executor._semaphore = None
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            from app.services.ps_executor import run_ps
            results = await asyncio.gather(
                *[run_ps("Get-Item", parse_json=False) for _ in range(N * 3)],
                return_exceptions=True,
            )

        ps_executor._semaphore = None
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"Unexpected errors: {errors}"


# ─── Error isolation ──────────────────────────────────────────────────────────

class TestErrorIsolation:

    async def test_single_scope_failure_does_not_affect_others(self):
        """When one scope's GET raises, other scopes still return 200."""
        good_networks = [_network(i) for i in range(10)]
        bad_network = _network(99)
        all_networks = [bad_network] + good_networks

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            if scope_id == bad_network:
                raise PowerShellExecutionError("cmd", "Access denied", 1)
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                responses = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in all_networks)
                )

        by_network = dict(zip(all_networks, responses))
        assert by_network[bad_network].status_code == 500
        for nw in good_networks:
            assert by_network[nw].status_code == 200, f"{nw} should be 200"

    async def test_timeout_on_one_scope_does_not_affect_others(self):
        """PowerShellTimeoutError on one scope → 504; others unaffected."""
        good_networks = [_network(i) for i in range(5)]
        timeout_network = _network(50)
        all_networks = [timeout_network] + good_networks

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            if scope_id == timeout_network:
                raise PowerShellTimeoutError("cmd", 60)
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                responses = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in all_networks)
                )

        by_network = dict(zip(all_networks, responses))
        assert by_network[timeout_network].status_code == 504
        for nw in good_networks:
            assert by_network[nw].status_code == 200

    async def test_random_failures_mixed_in_with_successes(self):
        """30 requests with 10 random failures — failures 500, successes 200."""
        random.seed(42)
        networks = [_network(i) for i in range(30)]
        fail_set = set(random.sample(networks, k=10))

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            if scope_id in fail_set:
                raise PowerShellExecutionError("cmd", "err", 1)
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                responses = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in networks)
                )

        by_network = dict(zip(networks, responses))
        for nw in fail_set:
            assert by_network[nw].status_code == 500
        for nw in set(networks) - fail_set:
            assert by_network[nw].status_code == 200


# ─── Mixed verb workload ──────────────────────────────────────────────────────

class TestMixedVerbWorkload:

    async def test_mixed_get_post_put_delete_50_scopes(self):
        """Simulate a Crossplane reconcile loop: GET → POST → PUT → GET → DELETE."""
        scopes = [_network(i) for i in range(50)]

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            return _make_scope(scope_id)

        async def mock_create(payload: DhcpScopePayload) -> DhcpScopePayload:
            return payload

        async def mock_update(scope_id: str, payload: DhcpScopePayload) -> DhcpScopePayload:
            return payload

        async def mock_delete(scope_id: str) -> None:
            return None

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get), \
                 patch("app.services.scope_service.create_scope", side_effect=mock_create), \
                 patch("app.services.scope_service.update_scope", side_effect=mock_update), \
                 patch("app.services.scope_service.delete_scope", side_effect=mock_delete):

                async def reconcile_one(network: str, op: str):
                    if op == "get":
                        r = await client.get(f"/api/v1/scopes/{network}")
                        return op, r.status_code
                    elif op == "post":
                        r = await client.post(f"/api/v1/scopes/{network}", json=_scope_body(network))
                        return op, r.status_code
                    elif op == "put":
                        r = await client.put(f"/api/v1/scopes/{network}", json=_scope_body(network))
                        return op, r.status_code
                    elif op == "delete":
                        r = await client.delete(f"/api/v1/scopes/{network}")
                        return op, r.status_code

                tasks = [
                    reconcile_one(network, op)
                    for network in scopes
                    for op in ["get", "post", "put", "get", "delete"]
                ]
                all_results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = [r for r in all_results if isinstance(r, Exception)]
        assert not errors, f"Unexpected exceptions: {errors[:3]}"

        for op, status in all_results:
            if op == "get":
                assert status == 200, f"GET returned {status}"
            elif op == "post":
                assert status == 200, f"POST returned {status}"
            elif op == "put":
                assert status == 200, f"PUT returned {status}"
            elif op == "delete":
                assert status == 204, f"DELETE returned {status}"

    async def test_list_endpoint_under_concurrent_load(self):
        """GET /api/v1/scopes (list) invoked 50 times concurrently must all succeed."""
        from app.models import DhcpScopeListResponse
        scope_list = [_make_scope(_network(i)) for i in range(20)]

        async def mock_list() -> DhcpScopeListResponse:
            return DhcpScopeListResponse(scopes=scope_list, errors=[])

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.list_scopes", side_effect=mock_list):
                results = await asyncio.gather(*(client.get("/api/v1/scopes") for _ in range(50)))

        assert all(r.status_code == 200 for r in results)


# ─── No pending tasks / un-awaited coroutines ─────────────────────────────────

class TestNoPendingTasks:

    async def test_all_tasks_complete_after_concurrent_gets(self):
        """After 50 concurrent GETs, there must be no dangling asyncio tasks."""
        networks = [_network(i) for i in range(50)]
        before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                await asyncio.gather(*(client.get(f"/api/v1/scopes/{n}") for n in networks))

        after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        leaked = after - before
        assert not leaked, f"Leaked tasks: {leaked}"

    async def test_cancelled_tasks_do_not_leave_held_locks(self):
        """Tasks cancelled mid-lock must release the lock so subsequent tasks proceed."""
        manager = ScopeLockManager()
        lock_held = asyncio.Event()
        proceed = asyncio.Event()

        async def holder():
            async with manager.lock("stress-scope"):
                lock_held.set()
                await proceed.wait()

        async def waiter():
            async with manager.lock("stress-scope"):
                return "ok"

        holder_task = asyncio.create_task(holder())
        await lock_held.wait()
        holder_task.cancel()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass

        result = await asyncio.wait_for(waiter(), timeout=1.0)
        assert result == "ok"


# ─── Deterministic results under random delays ────────────────────────────────

class TestDeterministicResults:

    async def test_results_deterministic_under_random_delays(self):
        """Even with random mock delays, each scope's result must match its expected shape."""
        networks = [_network(i) for i in range(30)]
        random.seed(7)
        delays = {n: random.uniform(0, 0.02) for n in networks}

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            await asyncio.sleep(delays[scope_id])
            return _make_scope(scope_id)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                responses = await asyncio.gather(
                    *(client.get(f"/api/v1/scopes/{n}") for n in networks)
                )

        by_network = dict(zip(networks, responses))
        for network in networks:
            r = by_network[network]
            assert r.status_code == 200, f"{network} → {r.status_code}"
            body = r.json()
            assert body["network"] == network
            assert body["subnetMask"] == "255.255.255.0"

    async def test_repeated_get_observe_loop_stable(self):
        """Simulate Crossplane's 60s observe loop: 20 repeated GETs for same scope are identical."""
        network = "10.20.30.0"
        scope = _make_scope(network)
        responses = []

        async def mock_get(scope_id: str) -> DhcpScopePayload:
            return scope

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.scope_service.get_scope", side_effect=mock_get):
                for _ in range(20):
                    r = await client.get(f"/api/v1/scopes/{network}")
                    assert r.status_code == 200
                    responses.append(r.json())

        first = responses[0]
        for i, resp in enumerate(responses[1:], 1):
            assert resp == first, f"Response {i} differs from response 0"
