"""Tests for the on-demand test runner (POST /api/v1/test-runs).

The subprocess is always mocked — this is the hermetic suite, and a test that
actually forked pytest would recurse.

Three of these are the executable form of the guards the feature rests on, and are
the reason this file exists rather than the endpoint being taken on trust:

  * the child environment cannot inherit this pod's DHCP settings
  * a protected host is refused before anything starts
  * captured output has the run's password stripped out of it

The rest cover the request contract.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.models import TestRunRequest, TestTarget
from app.services import test_runner


TARGET = {
    "dhcpServerHost": "dhcp-test.lab.local",
    "winrmUsername": "svc-test",
    "winrmPassword": "hunter2-correct-horse",
    "winrmAuth": "ntlm",
}


def _target(**overrides) -> TestTarget:
    return TestTarget(**{**TARGET, **overrides})


@pytest.fixture(autouse=True)
def _clean_registry():
    test_runner._reset_for_tests()
    yield
    test_runner._reset_for_tests()


async def _request(method: str, path: str, **kwargs):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


# ---------------------------------------------------------------------------
# Guard 1 — the child environment is built, never inherited
# ---------------------------------------------------------------------------

class TestChildEnvironment:

    def test_target_fields_become_dhcp_env(self):
        env = test_runner._child_env(_target())
        assert env["DHCP_SERVER_HOST"] == "dhcp-test.lab.local"
        assert env["DHCP_TRANSPORT"] == "psrp"
        assert env["DHCP_IT"] == "1"
        assert env["WINRM_USERNAME"] == "svc-test"
        assert env["WINRM_PASSWORD"] == "hunter2-correct-horse"

    def test_pod_dhcp_settings_are_scrubbed_not_inherited(self):
        """The guard that matters most.

        If os.environ leaked through, an omitted or mistyped target field would
        fall back to the server this deployment manages — and the live suite
        creates and deletes real scopes.
        """
        with patch.dict(
            "os.environ",
            {
                "DHCP_SERVER_HOST": "dhcp-prod.lab.local",
                "WINRM_PASSWORD": "production-secret",
                "WINRM_USERNAME": "svc-prod",
                "DHCP_API_TOKEN": "tok",
            },
            clear=False,
        ):
            env = test_runner._child_env(_target())

        assert env["DHCP_SERVER_HOST"] == "dhcp-test.lab.local"
        assert env["WINRM_PASSWORD"] == "hunter2-correct-horse"
        assert env["WINRM_USERNAME"] == "svc-test"
        assert "production-secret" not in env.values()

    def test_hermetic_run_gets_no_dhcp_or_winrm_variables_at_all(self):
        with patch.dict(
            "os.environ",
            {"DHCP_SERVER_HOST": "dhcp-prod.lab.local", "WINRM_PASSWORD": "p"},
            clear=False,
        ):
            env = test_runner._child_env(None)

        leaked = [k for k in env if k.startswith(("DHCP_", "WINRM_"))]
        assert leaked == [], f"hermetic run inherited {leaked}"

    def test_unrelated_environment_is_preserved(self):
        """Scrubbing is targeted — PATH and friends must survive or python won't run."""
        with patch.dict("os.environ", {"PATH": "/usr/bin", "LANG": "C"}, clear=False):
            env = test_runner._child_env(_target())
        assert env["PATH"] == "/usr/bin"
        assert env["LANG"] == "C"


# ---------------------------------------------------------------------------
# Guard 2 — protected hosts are refused
# ---------------------------------------------------------------------------

class TestProtectedHosts:

    def test_this_deployments_own_dhcp_server_is_refused(self):
        with patch.object(settings, "DHCP_SERVER_HOST", "dhcp-prod.lab.local"):
            from app.errors import TestTargetRefusedError

            with pytest.raises(TestTargetRefusedError):
                test_runner.assert_target_allowed(_target(dhcpServerHost="dhcp-prod.lab.local"))

    def test_refusal_is_case_insensitive(self):
        """Hostnames are case-insensitive; the guard must not be bypassable by shift key."""
        with patch.object(settings, "DHCP_SERVER_HOST", "dhcp-prod.lab.local"):
            from app.errors import TestTargetRefusedError

            with pytest.raises(TestTargetRefusedError):
                test_runner.assert_target_allowed(_target(dhcpServerHost="DHCP-PROD.LAB.LOCAL"))

    def test_deny_list_covers_a_deployment_with_no_dhcp_server_of_its_own(self):
        """The test release manages nothing, so the implicit rule has nothing to match."""
        with patch.object(settings, "DHCP_SERVER_HOST", ""), patch.object(
            settings, "TEST_RUNNER_DENY_HOSTS", "dhcp-prod.lab.local, dhcp-dr.lab.local"
        ):
            from app.errors import TestTargetRefusedError

            with pytest.raises(TestTargetRefusedError):
                test_runner.assert_target_allowed(_target(dhcpServerHost="dhcp-dr.lab.local"))

    def test_an_ordinary_test_server_is_allowed(self):
        with patch.object(settings, "DHCP_SERVER_HOST", "dhcp-prod.lab.local"):
            test_runner.assert_target_allowed(_target())  # must not raise

    @pytest.mark.asyncio
    async def test_refused_target_returns_422_and_starts_nothing(self):
        with patch.object(settings, "DHCP_SERVER_HOST", "dhcp-prod.lab.local"), patch(
            "asyncio.create_subprocess_exec", new=AsyncMock()
        ) as spawn:
            resp = await _request(
                "POST",
                "/api/v1/test-runs",
                json={"suite": "all", "target": {**TARGET, "dhcpServerHost": "dhcp-prod.lab.local"}},
            )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "TEST_TARGET_REFUSED"
        spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Guard 4 — output redaction
# ---------------------------------------------------------------------------

class TestRedaction:

    def test_run_password_is_stripped_from_captured_output(self):
        target = _target()
        leaked = (
            "E  pypsrp.exceptions.AuthenticationError: failed for "
            "svc-test / hunter2-correct-horse at dhcp-test.lab.local"
        )
        assert "hunter2-correct-horse" not in test_runner._redact(leaked, target)
        assert "***" in test_runner._redact(leaked, target)

    def test_pod_credentials_are_stripped_too(self):
        with patch.object(settings, "WINRM_PASSWORD", "production-secret"), patch.object(
            settings, "DHCP_API_TOKEN", "api-token-value"
        ):
            out = test_runner._redact("saw production-secret and api-token-value", None)
        assert "production-secret" not in out
        assert "api-token-value" not in out

    def test_ordinary_output_is_untouched(self):
        text = "687 passed, 30 skipped in 2.11s"
        assert test_runner._redact(text, _target()) == text


# ---------------------------------------------------------------------------
# Request contract
# ---------------------------------------------------------------------------

class TestRequestContract:

    def test_live_suite_requires_a_target(self):
        with pytest.raises(ValueError, match="required"):
            TestRunRequest(suite="live")

    def test_all_suite_requires_a_target(self):
        with pytest.raises(ValueError, match="required"):
            TestRunRequest(suite="all")

    def test_hermetic_suite_rejects_a_target(self):
        """A password sent for a suite that cannot use it crossed the wire for nothing."""
        with pytest.raises(ValueError, match="must be omitted"):
            TestRunRequest(suite="hermetic", target=_target())

    def test_hermetic_is_the_default(self):
        assert TestRunRequest().suite == "hermetic"

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValueError):
            TestRunRequest(suite="hermetic", nonsense=1)

    @pytest.mark.parametrize(
        "suite,expected",
        [
            ("hermetic", "--ignore=tests/integration"),
            ("live", "tests/integration"),
        ],
    )
    def test_suite_maps_to_pytest_args(self, suite, expected):
        assert expected in test_runner._pytest_args(suite)

    def test_all_suite_restricts_nothing(self):
        args = test_runner._pytest_args("all")
        assert "--ignore=tests/integration" not in args
        assert "tests/integration" not in args

    def test_tracebacks_are_truncated_in_every_suite(self):
        """--tb=line: a full traceback can echo locals, and a connection repr
        carries the credential."""
        for suite in ("hermetic", "live", "all"):
            assert "--tb=line" in test_runner._pytest_args(suite)


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------

class TestEndpoints:

    @pytest.mark.asyncio
    async def test_accepted_run_returns_202_and_a_run_id(self):
        with patch.object(test_runner, "_execute", new=AsyncMock()):
            resp = await _request("POST", "/api/v1/test-runs", json={"suite": "hermetic"})

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "running"
        assert body["runId"]
        assert body["targetHost"] is None

    @pytest.mark.asyncio
    async def test_second_run_while_one_is_active_returns_409(self):
        test_runner._active_run_id = "already-running"
        try:
            resp = await _request("POST", "/api/v1/test-runs", json={"suite": "hermetic"})
        finally:
            test_runner._active_run_id = None

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TEST_RUN_IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_unknown_run_id_returns_404(self):
        resp = await _request("GET", "/api/v1/test-runs/deadbeef")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TEST_RUN_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_list_omits_captured_output(self):
        """Summaries are for scanning; output is fetched per run, after redaction."""
        with patch.object(test_runner, "_execute", new=AsyncMock()):
            await _request("POST", "/api/v1/test-runs", json={"suite": "hermetic"})

        resp = await _request("GET", "/api/v1/test-runs")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 1
        assert "output" not in runs[0]

    @pytest.mark.asyncio
    async def test_a_stored_run_never_carries_credentials(self):
        """The registry holds the host, never the account or the password."""
        with patch.object(settings, "DHCP_SERVER_HOST", "dhcp-prod.lab.local"), patch.object(
            test_runner, "_execute", new=AsyncMock()
        ):
            resp = await _request(
                "POST", "/api/v1/test-runs", json={"suite": "all", "target": TARGET}
            )

        assert resp.status_code == 202
        serialized = resp.text
        assert "hunter2-correct-horse" not in serialized
        assert "svc-test" not in serialized
        assert resp.json()["targetHost"] == "dhcp-test.lab.local"

    @pytest.mark.asyncio
    async def test_requires_a_token_when_one_is_configured(self):
        with patch.object(settings, "DHCP_API_TOKEN", "s3cret"):
            resp = await _request("POST", "/api/v1/test-runs", json={"suite": "hermetic"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_accepts_a_valid_token(self):
        with patch.object(settings, "DHCP_API_TOKEN", "s3cret"), patch.object(
            test_runner, "_execute", new=AsyncMock()
        ):
            resp = await _request(
                "POST",
                "/api/v1/test-runs",
                json={"suite": "hermetic"},
                headers={"Authorization": "Bearer s3cret"},
            )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_route_does_not_require_a_working_dhcp_environment(self):
        """The test release manages no scopes of its own and must still run tests.

        The scope routes carry require_dhcp_service; these deliberately do not.
        """
        from app.errors import DhcpEnvironmentError, DhcpEnvReason

        with patch(
            "app.services.dhcp_service.validate_dhcp_environment",
            side_effect=DhcpEnvironmentError(DhcpEnvReason.UNSUPPORTED_OS, "linux"),
        ), patch.object(test_runner, "_execute", new=AsyncMock()):
            resp = await _request("POST", "/api/v1/test-runs", json={"suite": "hermetic"})

        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

class TestRunLifecycle:
    """The subprocess is faked here, but _execute really runs.

    Note these await wait_for_active_run() rather than sleeping. Guessing at
    event-loop scheduling is how the first version of this file deadlocked: the
    background task outlived the patch context, called the *real*
    create_subprocess_exec, and forked pytest into itself.
    """

    @staticmethod
    def _fake_process(stdout: bytes, returncode: int) -> AsyncMock:
        process = AsyncMock()
        process.communicate.return_value = (stdout, b"")
        process.returncode = returncode
        return process

    @pytest.mark.asyncio
    async def test_a_passing_run_is_recorded_with_its_output(self):
        process = self._fake_process(b"687 passed, 30 skipped in 2.11s\n", 0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            run = await test_runner.start_run(TestRunRequest(suite="hermetic"))
            await test_runner.wait_for_active_run()

        finished = test_runner.get_run(run.runId)
        assert finished.status == "passed"
        assert finished.exitCode == 0
        assert "687 passed" in finished.output
        assert finished.durationSeconds is not None

    @pytest.mark.asyncio
    async def test_a_failing_run_is_recorded_as_failed(self):
        process = self._fake_process(b"1 failed, 686 passed\n", 1)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            run = await test_runner.start_run(TestRunRequest(suite="hermetic"))
            await test_runner.wait_for_active_run()

        assert test_runner.get_run(run.runId).status == "failed"
        assert test_runner.get_run(run.runId).exitCode == 1

    @pytest.mark.asyncio
    async def test_the_slot_is_released_after_a_run(self):
        """Otherwise the first run would permanently 409 every later one."""
        process = self._fake_process(b"ok\n", 0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            await test_runner.start_run(TestRunRequest(suite="hermetic"))
            await test_runner.wait_for_active_run()

        assert test_runner._active_run_id is None

    @pytest.mark.asyncio
    async def test_the_slot_is_released_even_when_the_run_errors(self):
        """A crash that leaked the slot would wedge the endpoint at 409 forever."""
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no exec")):
            run = await test_runner.start_run(TestRunRequest(suite="hermetic"))
            await test_runner.wait_for_active_run()

        assert test_runner._active_run_id is None
        assert test_runner.get_run(run.runId).status == "error"

    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_subprocess_and_is_reported(self):
        process = AsyncMock()
        process.communicate.side_effect = asyncio.TimeoutError()
        process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch.object(settings, "TEST_RUNNER_TIMEOUT_SECONDS", 60):
            run = await test_runner.start_run(TestRunRequest(suite="hermetic"))
            await test_runner.wait_for_active_run()

        finished = test_runner.get_run(run.runId)
        assert finished.status == "timeout"
        assert "60" in finished.output
        process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_registry_is_bounded(self):
        """A long-lived pod must not accumulate runs without limit."""
        process = self._fake_process(b"ok\n", 0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            for _ in range(test_runner._MAX_RUNS + 3):
                await test_runner.start_run(TestRunRequest(suite="hermetic"))
                await test_runner.wait_for_active_run()

        assert len(test_runner._runs) == test_runner._MAX_RUNS
