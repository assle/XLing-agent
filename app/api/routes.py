from fastapi import APIRouter

from app.api.account import router as account_router
from app.api.admin import router as admin_router
from app.api.support import router as support_router
from app.api.system import health
from app.api.system import router as system_router

router = APIRouter()
router.include_router(system_router)
router.include_router(account_router)
router.include_router(support_router)
router.include_router(admin_router)


__all__ = ["health", "router"]
