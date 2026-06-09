# backend/app/api/superadmin/router.py
from fastapi import APIRouter

from app.api.superadmin.users import router as users_router
from app.api.superadmin.organizations import router as orgs_router
from app.api.superadmin.projects import router as projects_router
from app.api.superadmin.billing import router as billing_router
from app.api.superadmin.staff import router as staff_router
from app.api.superadmin.audit import router as audit_router
from app.api.superadmin.flags import router as flags_router
from app.api.superadmin.metrics import router as metrics_router

router = APIRouter(tags=["Superadmin"])

router.include_router(users_router)
router.include_router(orgs_router)
router.include_router(projects_router)
router.include_router(billing_router)
router.include_router(staff_router)
router.include_router(audit_router)
router.include_router(flags_router)
router.include_router(metrics_router)