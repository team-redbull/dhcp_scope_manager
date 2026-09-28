import hmac

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.errors import UnauthorizedError
from app.utils.decorators import log_call

# auto_error=False: a deployment with no DHCP_API_TOKEN must stay a no-op below,
# so a missing/malformed header cannot be allowed to raise here on its own — it
# has to fall through to the settings check first. This also registers a proper
# OpenAPI securityScheme (bearerAuth), which is what gets Swagger UI's docs to
# attach the header on "Try it out" — a plain Header() parameter left it
# rendered but not wired into the actual request.
_bearer_scheme = HTTPBearer(auto_error=False)


@log_call
async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    if not settings.DHCP_API_TOKEN:
        return

    token = credentials.credentials if credentials else ""
    if not hmac.compare_digest(token, settings.DHCP_API_TOKEN):
        raise UnauthorizedError()
