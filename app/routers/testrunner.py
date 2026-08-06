"""On-demand execution of this service's own test suite.

Note what is deliberately absent from the router: `require_dhcp_service`. The scope
routes carry it because they drive PowerShell on *this* deployment's DHCP server —
these routes never do. The suite runs in a subprocess pointed at a server named in
the request, so a deployment that manages no scopes of its own (returning 503 from
every scope route) can still run tests perfectly well.

`verify_token` is present, and matters more here than anywhere else: this route
executes code. Note it is a no-op when DHCP_API_TOKEN is unset — the chart always
generates one, but an image run without it has an open endpoint.
"""
from fastapi import APIRouter, Depends, status

from app.dependencies.auth import verify_token
from app.models import TestRun, TestRunListResponse, TestRunRequest
from app.services import test_runner
from app.utils.decorators import log_call


router = APIRouter(
    prefix="/api/v1",
    tags=["test-runs"],
    dependencies=[Depends(verify_token)],
)


@router.post("/test-runs", response_model=TestRun, status_code=status.HTTP_202_ACCEPTED)
@log_call
async def start_test_run(request: TestRunRequest) -> TestRun:
    """Accept a run and return its id immediately.

    202, not 200: the suite takes minutes against a real server, far longer than an
    HTTP request should be held open, so the result is collected by polling
    GET /test-runs/{runId}.
    """
    return await test_runner.start_run(request)


@router.get("/test-runs", response_model=TestRunListResponse, status_code=status.HTTP_200_OK)
@log_call
async def list_test_runs() -> TestRunListResponse:
    return TestRunListResponse(runs=test_runner.list_runs())


@router.get("/test-runs/{run_id}", response_model=TestRun, status_code=status.HTTP_200_OK)
@log_call
async def get_test_run(run_id: str) -> TestRun:
    return test_runner.get_run(run_id)
