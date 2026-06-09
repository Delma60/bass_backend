import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(
    subject: str,
    project_id: str,
    extra_claims: dict | None = None,
    expires_delta: int | None = None,
) -> str:
    exp_seconds = expires_delta or settings.jwt_expiry
    expire = datetime.now(timezone.utc) + timedelta(seconds=exp_seconds)
    payload: dict = {
        "sub": subject,
        "aud": f"proj_{project_id}" if not project_id.startswith("proj_") else project_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(subject: str, project_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=30)
    payload = {
        "sub": subject,
        "aud": f"proj_{project_id}" if not project_id.startswith("proj_") else project_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_project_token(token: str, project_id: str) -> dict:
    audience = f"proj_{project_id}" if not project_id.startswith("proj_") else project_id
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience=audience)
    except JWTError as e:
        logger.debug("Token decode failed: %s", e)
        raise