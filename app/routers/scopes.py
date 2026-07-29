from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies.auth import verify_token
from app.dependencies.dhcp import require_dhcp_service
from app.dependencies.scopes import validate_scope, validate_scope_request
from app.models import DhcpScopeBody, DhcpScopeListResponse, DhcpScopePayload
from app.services import scope_service
from app.utils.decorators import log_call


router = APIRouter(
    prefix="/api/v1",
    tags=["scopes"],
    dependencies=[Depends(verify_token), Depends(require_dhcp_service)],
)


@router.get("/scopes", response_model=DhcpScopeListResponse, status_code=status.HTTP_200_OK)
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


@router.get("/scopes/{scope}", response_model=DhcpScopeBody, status_code=status.HTTP_200_OK)
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
