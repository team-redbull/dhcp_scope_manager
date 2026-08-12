from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies.auth import verify_token
from app.dependencies.dhcp import require_dhcp_service
from app.dependencies.scopes import validate_scope, validate_scope_request
from app.models import DhcpScopeBody, DhcpScopeListResponse, DhcpScopePayload
from app.services import scope_service
from app.utils.decorators import log_call


# Writes are authenticated, reads are not — CLAUDE.md section 10 records the
# trade-off and why it was taken.
#
# TWO routers rather than per-route dependencies, because FastAPI cannot subtract
# a router-level dependency from one route. This way the SAFE case is the default:
# a new route added to `router` inherits verify_token, and anonymity has to be
# chosen deliberately by putting the route on `read_router`. Getting that backwards
# — one router with per-route auth — makes a forgotten Depends an open write path.
#
# require_dhcp_service stays on BOTH: a read still talks to the DHCP server, so a
# deployment with no server configured must 503 rather than error out mid-cmdlet.
#
# test_route_auth_matrix pins the exact set of anonymous routes.
router = APIRouter(
    prefix="/api/v1",
    tags=["scopes"],
    dependencies=[Depends(verify_token), Depends(require_dhcp_service)],
)

read_router = APIRouter(
    prefix="/api/v1",
    tags=["scopes"],
    dependencies=[Depends(require_dhcp_service)],
)


@read_router.get(
    "/scopes", response_model=DhcpScopeListResponse, status_code=status.HTTP_200_OK
)
@log_call
async def list_scopes() -> DhcpScopeListResponse:
    return await scope_service.list_scopes()


@router.post("/scopes/{scope}", response_model=DhcpScopeBody, status_code=status.HTTP_200_OK)
@log_call
async def create_scope(
    payload: Annotated[DhcpScopePayload, Depends(validate_scope_request)],
) -> DhcpScopeBody:
    created = await scope_service.create_scope(payload)
    return created.body()


@read_router.get(
    "/scopes/{scope}", response_model=DhcpScopeBody, status_code=status.HTTP_200_OK
)
@log_call
async def get_scope(
    scope: str = Depends(validate_scope),
) -> DhcpScopeBody:
    current = await scope_service.get_scope(scope)
    return current.body()


@router.put("/scopes/{scope}", response_model=DhcpScopeBody, status_code=status.HTTP_200_OK)
@log_call
async def update_scope(
    payload: Annotated[DhcpScopePayload, Depends(validate_scope_request)],
    scope: str = Depends(validate_scope),
) -> DhcpScopeBody:
    updated = await scope_service.update_scope(scope, payload)
    return updated.body()


@router.delete("/scopes/{scope}", status_code=status.HTTP_204_NO_CONTENT)
@log_call
async def delete_scope(
    scope: str = Depends(validate_scope),
) -> Response:
    await scope_service.delete_scope(scope)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Deleted-Scope": scope},
    )
