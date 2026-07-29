from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address
from typing import Annotated

from fastapi import Body, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.errors import InvalidScopeError
from app.models import DhcpScopeBody, DhcpScopePayload
from app.utils.decorators import log_call


@log_call
async def validate_scope(scope: str) -> str:
    try:
        return str(IPv4Address(scope))
    except (AddressValueError, ValueError):
        raise InvalidScopeError(scope)


@log_call
async def validate_scope_request(
    body: Annotated[DhcpScopeBody, Body(...)],
    scope: str = Depends(validate_scope),
) -> DhcpScopePayload:
    """Combine the path address with the request body into the domain model.

    The scope address exists only in the path, so there is nothing to
    cross-check — but subnet consistency still needs both halves, which is why
    the full model is assembled here rather than in the router.

    Cross-field failures (a range outside the path subnet, for example) are only
    detectable at this point. They are re-raised as RequestValidationError so
    they surface as the same 422 shape as any other bad body, rather than
    escaping as an unhandled ValidationError and becoming a 500.
    """
    data = body.model_dump()
    # gateway has three states the payload must tell apart — omitted (derive .254),
    # explicitly null/"" (no DHCP option 3), and an explicit address — but model_dump()
    # flattens the first two into the same None. Dropping the key restores the omission
    # so DhcpScopePayload.resolve_default_gateway can see it in model_fields_set.
    #
    # subnetMask needs no equivalent: it has no 'unset' meaning, so its before-validator
    # already maps omitted/null/"" to the same default and popping would change nothing.
    if "gateway" not in body.model_fields_set:
        data.pop("gateway")
    try:
        return DhcpScopePayload(scope=scope, **data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
