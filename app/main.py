from __future__ import annotations
import os
import sys

# Allow running directly from project root or from inside app/:
#   python app/main.py   (from project root)
#   python main.py       (from inside app/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.config import settings
from app.logging_config import configure_logging
from app.exception_handlers import register_exception_handlers
from app.routers import router

configure_logging(settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Release pooled PSRP runspaces so WinRM sessions on the DHCP server are
    # torn down on shutdown rather than left to time out. No-op for the local
    # transport, which holds nothing between calls.
    from app.services.ps_transport import get_transport

    aclose = getattr(get_transport(), "aclose", None)
    if aclose is not None:
        await aclose()


app = FastAPI(
    title="DHCP Scope Management API",
    version="1.0.0",
    description=(
        "Manages Windows DHCP scopes via PowerShell cmdlets, either locally or "
        "on a remote Windows DHCP server over PSRP. "
        "Consumed exclusively by Crossplane provider-http."
    ),
    lifespan=lifespan,
)

app.include_router(router)

register_exception_handlers(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=False,
    )
