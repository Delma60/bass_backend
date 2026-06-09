# backend/app/middleware/staff_auth.py
import logging
from enum import Enum

from fastapi import Depends, Header, HTTPException

from app.auth.staff_auth import verify_staff_token
from app.config import settings
from app.models.staff import StaffContext, StaffRole

logger = logging.getLogger(__name__)

ROLE_HIERARCHY: dict[StaffRole, int] = {
    StaffRole.support:     0,
    StaffRole.billing:     1,
    StaffRole.ops:         2,
    StaffRole.super_admin: 3,
}



async def get_staff_context(
    x_staff_token: str = Header(..., alias="x-staff-token"),
    x_internal_secret: str = Header(..., alias="x-internal-secret"),
) -> StaffContext:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")
    return await verify_staff_token(x_staff_token)


def require_staff_role(minimum_role: StaffRole):
    """FastAPI dependency — enforces minimum staff role."""
    async def check(staff: StaffContext = Depends(get_staff_context)) -> StaffContext:
        if ROLE_HIERARCHY.get(staff.role, -1) < ROLE_HIERARCHY[minimum_role]:
            raise HTTPException(
                status_code=403,
                detail=f"Requires {minimum_role.value} role or higher",
            )
        return staff
    return check