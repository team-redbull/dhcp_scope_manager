from __future__ import annotations

from app.services import dhcp_service
from app.utils.decorators import log_call


@log_call
async def require_dhcp_service() -> None:
    await dhcp_service.validate_dhcp_environment()
