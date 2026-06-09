from enum import Enum

from pydantic import BaseModel


class StaffRole(str, Enum):
    super_admin = "super_admin"
    ops = "ops"
    billing = "billing"
    support = "support"


class StaffContext(BaseModel):
    id: str
    email: str
    name: str
    role: StaffRole
    is_active: bool = True