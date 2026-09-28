from __future__ import annotations

from pydantic import BaseModel

from app.models.scope import DhcpScopePayload


class DhcpScopeListError(BaseModel):
    """Describes one scope that could not be assembled during GET /scopes."""

    scope: str
    error: str


class DhcpScopeListResponse(BaseModel):
    """Response model for GET /api/v1/scopes.

    Always returns HTTP 200.  Valid scopes appear in `scopes`; scopes whose
    data failed assembly (Pydantic validation error, missing DNS option, etc.)
    appear in `errors` so the caller can see which scope is broken without
    losing the rest of the list.
    """

    scopes: list[DhcpScopePayload]
    errors: list[DhcpScopeListError]
