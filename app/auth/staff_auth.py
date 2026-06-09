import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.models.staff import StaffContext, StaffRole

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_staff_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_staff_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def issue_staff_token(staff_id: str, email: str, name: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.staff_jwt_expiry)
    payload = {
        "sub": staff_id,
        "email": email,
        "name": name,
        "role": role,
        "aud": "staff",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.staff_jwt_secret, algorithm="HS256")


async def verify_staff_token(token: str) -> StaffContext:
    try:
        payload = jwt.decode(
            token,
            settings.staff_jwt_secret,
            algorithms=["HS256"],
            audience="staff",
        )
        return StaffContext(
            id=payload["sub"],
            email=payload["email"],
            name=payload["name"],
            role=StaffRole(payload["role"]),
        )
    except (JWTError, KeyError, ValueError) as e:
        logger.debug("Staff token decode failed: %s", e)
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid staff token")