from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies.auth import verify_token
from app.dependencies.dhcp import require_dhcp_service
from app.dependencies.scopes import validate_scope_id, validate_scope_request
from app.models import DhcpScopeListResponse, DhcpScopePayload
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


@router.post("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
@log_call
async def create_scope_by_id(
    scope_and_payload: Annotated[tuple[str, DhcpScopePayload], Depends(validate_scope_request)],
) -> DhcpScopePayload:
    scope_id, payload = scope_and_payload
    return await scope_service.create_scope(payload)


@router.get("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
@log_call
async def get_scope(
    scope_id: str = Depends(validate_scope_id),
) -> DhcpScopePayload:
    return await scope_service.get_scope(scope_id)


@router.put("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
@log_call
async def update_scope(
    scope_and_payload: Annotated[tuple[str, DhcpScopePayload], Depends(validate_scope_request)],
    scope_id: str = Depends(validate_scope_id),
) -> DhcpScopePayload:
    _, payload = scope_and_payload
    return await scope_service.update_scope(scope_id, payload)


@router.delete("/scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
@log_call
async def delete_scope(
    scope_id: str = Depends(validate_scope_id),
) -> Response:
    await scope_service.delete_scope(scope_id)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Deleted-Scope": scope_id},
    )
