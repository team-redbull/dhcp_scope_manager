"""Runs this service's own pytest suite in a subprocess, on demand.

Exists for the air-gapped environment, which has no CI: the image already carries
pytest (`requirements.txt`) and the suite (`COPY tests/` in the Dockerfile), so the
deployed API can verify itself against a real DHCP server without anything being
fetched, built, or transferred.

**Why a subprocess and not an in-process pytest call.** pytest drives its own event
loop, and the suite's own fixtures create an ASGI client against `app.main:app`.
Running that inside the live server's loop would nest one loop in another and
deadlock. A subprocess also gives the two properties that make this safe at all: a
hard kill on timeout, and an environment built from scratch rather than inherited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from app.config import settings
from app.errors import TestRunInProgressError, TestRunNotFoundError, TestTargetRefusedError
from app.models import TestRun, TestRunRequest, TestRunSummary, TestTarget

logger = logging.getLogger(__name__)

# Where the suite lives in the image (Dockerfile WORKDIR /app).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every environment key that could point the suite at a DHCP server. The child's
# environment is built by removing all of these and then setting only what the
# request asked for — so there is no path by which an omitted field silently falls
# back to whatever this pod is configured to manage.
_TARGET_ENV_PREFIXES = ("DHCP_", "WINRM_")

# Runs are kept in memory, newest last, and bounded so a long-lived pod cannot grow
# without limit. This is the state that makes a pod running the runner unsuitable
# for horizontal scaling — see TestRunNotFoundError.
_MAX_RUNS = 20
_runs: "OrderedDict[str, TestRun]" = OrderedDict()
_run_lock = asyncio.Lock()
_active_run_id: str | None = None

# asyncio holds only a weak reference to a running task, so a task nobody keeps a
# strong reference to can be garbage-collected mid-execution. Holding the handle
# also lets tests await the run instead of guessing at event-loop scheduling.
_active_task: "asyncio.Task | None" = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pytest_args() -> list[str]:
    """Arguments for the one and only kind of run: the whole suite.

    No path selection — every run collects both the mocked tests and
    tests/integration, and the live half actually executes because _child_env sets
    DHCP_IT. There is no way to ask for a subset, so a passing run always means the
    same thing.

    -p no:cacheprovider: /app is group-writable but writing .pytest_cache into the
    image's working directory is pointless noise.
    --tb=line: one line per failure. Full tracebacks can echo local variables, and
    a WinRM connection object's repr carries the credential.
    """
    return ["-q", "--tb=line", "-p", "no:cacheprovider"]


def _child_env(target: TestTarget) -> dict[str, str]:
    """Build the subprocess environment from scratch.

    Scrubbing first is the point. Inheriting os.environ would leave this pod's own
    DHCP_SERVER_HOST and WINRM_PASSWORD visible to the suite, and the live tests
    create and delete real scopes — a mistyped target field would then quietly fall
    back to whatever this deployment manages.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_TARGET_ENV_PREFIXES)
    }

    env.update(
        {
            "DHCP_TRANSPORT": "psrp",
            "DHCP_IT": "1",
            "DHCP_SERVER_HOST": target.dhcpServerHost,
            "WINRM_PORT": str(target.winrmPort),
            "WINRM_USE_SSL": str(target.winrmUseSsl).lower(),
            "WINRM_AUTH": target.winrmAuth,
            "WINRM_USERNAME": target.winrmUsername,
            "WINRM_PASSWORD": target.winrmPassword.get_secret_value(),
            "WINRM_CERT_VALIDATION": str(target.winrmCertValidation).lower(),
        }
    )
    return env


def _redact(text: str, target: TestTarget) -> str:
    """Strip this run's secrets from captured output before anyone can read it.

    pytest is not built to keep secrets. An assertion message, a connection error
    from pypsrp, or a stray print can carry the password even with --tb=line, and
    the output goes back over HTTP and into the logs.
    """
    password = target.winrmPassword.get_secret_value()
    if password:
        text = text.replace(password, "***")
    # The pod's own credential should never appear here — the child never receives
    # it — but redact it too rather than rely on that holding forever.
    if settings.WINRM_PASSWORD:
        text = text.replace(settings.WINRM_PASSWORD, "***")
    if settings.DHCP_API_TOKEN:
        text = text.replace(settings.DHCP_API_TOKEN, "***")
    return text


def assert_target_allowed(target: TestTarget) -> None:
    """Refuse a target this deployment must not run destructive tests against.

    Checked before anything is started, and against a set that always includes this
    pod's own DHCP_SERVER_HOST: every run includes the live tests, which create and
    delete scope 10.77.88.0 — not something to do on a server Crossplane is
    reconciling.
    """
    host = target.dhcpServerHost.strip().lower()
    if host in settings.protected_dhcp_hosts:
        raise TestTargetRefusedError(target.dhcpServerHost)


def _record(run: TestRun) -> None:
    _runs[run.runId] = run
    while len(_runs) > _MAX_RUNS:
        _runs.popitem(last=False)


async def _execute(run_id: str, request: TestRunRequest) -> None:
    """Run pytest to completion and replace the registry entry with the result."""
    global _active_run_id

    target = request.target
    started = _runs[run_id].startedAt
    t0 = time.monotonic()
    status = "error"
    exit_code: int | None = None
    output = ""

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            *_pytest_args(),
            cwd=_PROJECT_ROOT,
            env=_child_env(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), settings.TEST_RUNNER_TIMEOUT_SECONDS
            )
            output = stdout.decode(errors="replace")
            exit_code = process.returncode
            status = "passed" if exit_code == 0 else "failed"
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            status = "timeout"
            output = (
                f"Killed after {settings.TEST_RUNNER_TIMEOUT_SECONDS}s "
                f"(TEST_RUNNER_TIMEOUT_SECONDS)."
            )
    except Exception as exc:  # noqa: BLE001 — the result is reported, never raised
        logger.exception("Test run %s could not be executed", run_id)
        status = "error"
        output = f"Failed to start pytest: {type(exc).__name__}"
    finally:
        finished = _now()
        _record(
            TestRun(
                runId=run_id,
                status=status,
                startedAt=started,
                finishedAt=finished,
                durationSeconds=round(time.monotonic() - t0, 2),
                exitCode=exit_code,
                targetHost=target.dhcpServerHost,
                output=_redact(output, target),
            )
        )
        async with _run_lock:
            _active_run_id = None
        logger.info(
            "Test run finished",
            extra={"operation": "test_run", "status": status, "run_id": run_id},
        )


async def start_run(request: TestRunRequest) -> TestRun:
    """Accept a run and return immediately; the suite executes in the background.

    One at a time: the live tests own a single scope and delete it before and
    after every test, so two concurrent runs would delete each other's state.
    """
    global _active_run_id, _active_task

    assert_target_allowed(request.target)

    async with _run_lock:
        if _active_run_id is not None:
            raise TestRunInProgressError(_active_run_id)
        run_id = uuid.uuid4().hex[:12]
        _active_run_id = run_id

    run = TestRun(
        runId=run_id,
        status="running",
        startedAt=_now(),
        targetHost=request.target.dhcpServerHost,
    )
    _record(run)

    logger.info(
        "Test run started",
        extra={
            "operation": "test_run",
            "run_id": run_id,
            # The host only — never the account or the password.
            "target_host": request.target.dhcpServerHost,
        },
    )
    _active_task = asyncio.create_task(_execute(run_id, request))
    return run


async def wait_for_active_run() -> None:
    """Block until the running suite finishes. For tests and shutdown only."""
    if _active_task is not None:
        await asyncio.shield(_active_task)


def get_run(run_id: str) -> TestRun:
    run = _runs.get(run_id)
    if run is None:
        raise TestRunNotFoundError(run_id)
    return run


def list_runs() -> list[TestRunSummary]:
    """Newest first, without captured output."""
    return [
        TestRunSummary(**run.model_dump(exclude={"output"}))
        for run in reversed(_runs.values())
    ]


def _reset_for_tests() -> None:
    """Clear module state between unit tests."""
    global _active_run_id, _active_task
    _runs.clear()
    _active_run_id = None
    _active_task = None
