from fastapi import APIRouter

from app.routers.health import router as health_router
from app.routers.scopes import read_router as scopes_read_router
from app.routers.scopes import router as scopes_router

router = APIRouter()
router.include_router(scopes_router)
# The anonymous half of the scope routes — see the comment in app/routers/scopes.py.
router.include_router(scopes_read_router)
router.include_router(health_router)
