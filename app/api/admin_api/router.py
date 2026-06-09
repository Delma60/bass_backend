# backend/app/api/admin_api/router.py
"""
Mounts all /admin-api/* sub-routers.

Every route in this package requires:
  Authorization: Bearer <service_key>          — service-role API key
  X-Admin-Integration-Secret: <shared-secret>  — bilateral secret

This router is completely separate from:
  /v1/*         — public SDK API (api key only)
  /internal/*   — Next.js dashboard proxy (X-Internal-Secret)
  /superadmin/* — BaaS staff panel (staff JWT + X-Internal-Secret)

It is mounted in main.py at prefix="/admin-api".
"""
from fastapi import APIRouter

from app.api.admin_api.audit import router as audit_router
from app.api.admin_api.metrics import router as metrics_router
from app.api.admin_api.organizations import router as organizations_router
from app.api.admin_api.projects import router as projects_router
from app.api.admin_api.storage import router as storage_router
from app.api.admin_api.usage import router as usage_router
from app.api.admin_api.users import router as users_router
from app.api.admin_api.billing import router as billing_router

router = APIRouter(tags=["Admin API"])

router.include_router(metrics_router)
router.include_router(users_router)
router.include_router(organizations_router)
router.include_router(projects_router)
router.include_router(usage_router)
router.include_router(storage_router)
router.include_router(audit_router)
router.include_router(billing_router)