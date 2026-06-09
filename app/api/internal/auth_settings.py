# backend/app/api/internal/auth_settings.py
"""
Internal-only endpoints for per-project auth settings and email templates.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Auth Settings"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)

# ─── Default Templates ────────────────────────────────────────────────────────

DEFAULT_TEMPLATES = {
    "verification": {
        "subject": "Verify your email address",
        "body": "Hi {{name}},\n\nPlease verify your email address by clicking the link below:\n\n{{verification_url}}\n\nThis link expires in 24 hours.\n\nThanks,\n{{app_name}} Team",
    },
    "password_reset": {
        "subject": "Reset your password",
        "body": "Hi {{name}},\n\nSomeone requested a password reset for your account. Click the link below to reset it:\n\n{{reset_url}}\n\nThis link expires in 1 hour. If you didn't request this, you can safely ignore this email.\n\nThanks,\n{{app_name}} Team",
    },
    "email_change": {
        "subject": "Confirm your new email address",
        "body": "Hi {{name}},\n\nYou requested to change your email address to {{new_email}}. Click the link below to confirm:\n\n{{confirmation_url}}\n\nThis link expires in 24 hours.\n\nThanks,\n{{app_name}} Team",
    },
    "magic_link": {
        "subject": "Your sign-in link",
        "body": "Hi,\n\nClick the link below to sign in to your account:\n\n{{magic_url}}\n\nThis link expires in 15 minutes and can only be used once.\n\nThanks,\n{{app_name}} Team",
    },
}

DEFAULT_AUTH_SETTINGS = {
    "allow_signups": True,
    "require_email_verification": True,
    "allow_multiple_sessions": True,
    "min_password_length": 8,
    "session_duration_hours": 168,  # 7 days
    "providers": {
        "email": True,
        "phone": False,
        "magic_link": False,
        "google": False,
        "github": False,
    },
    "smtp": {
        "host": "",
        "port": 587,
        "user": "",
        "password": "",
        "from_name": "",
        "from_email": "",
        "secure": False,
    },
}

# ─── Auth Settings ────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/auth/settings", dependencies=[InternalGuard])
async def get_auth_settings(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get auth configuration for a project."""
    try:
        result = await db.execute(
            text("""
                SELECT settings_json, updated_at
                FROM project_auth_settings
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().first()
        if not row:
            return {"data": DEFAULT_AUTH_SETTINGS}
        merged = {**DEFAULT_AUTH_SETTINGS, **json.loads(row["settings_json"])}
        return {"data": merged}
    except Exception as e:
        logger.warning("project_auth_settings table may not exist: %s", e)
        return {"data": DEFAULT_AUTH_SETTINGS}


class UpdateAuthSettingsRequest(BaseModel):
    settings: dict[str, Any]


@router.put("/projects/{project_id}/auth/settings", dependencies=[InternalGuard])
async def update_auth_settings(
    project_id: str,
    body: UpdateAuthSettingsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upsert auth settings for a project."""
    try:
        await db.execute(
            text("""
                INSERT INTO project_auth_settings (id, project_id, settings_json, updated_at)
                VALUES (:id, :project_id, :settings_json, NOW())
                ON CONFLICT (project_id) DO UPDATE
                  SET settings_json = EXCLUDED.settings_json,
                      updated_at = NOW()
            """),
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "settings_json": json.dumps(body.settings),
            },
        )
        await db.commit()
        return {"data": {"saved": True}}
    except Exception as e:
        logger.error("Failed to save auth settings: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Email Templates ──────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/auth/templates", dependencies=[InternalGuard])
async def get_email_templates(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get all email templates for a project."""
    try:
        result = await db.execute(
            text("""
                SELECT template_key, subject, body, updated_at
                FROM project_email_templates
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        rows = {r["template_key"]: {"subject": r["subject"], "body": r["body"]} for r in result.mappings()}
        # Merge with defaults
        templates = {}
        for key, default in DEFAULT_TEMPLATES.items():
            templates[key] = rows.get(key, default)
        return {"data": templates}
    except Exception as e:
        logger.warning("project_email_templates table may not exist: %s", e)
        return {"data": DEFAULT_TEMPLATES}


class UpdateTemplateRequest(BaseModel):
    subject: str
    body: str


@router.put("/projects/{project_id}/auth/templates/{template_key}", dependencies=[InternalGuard])
async def update_email_template(
    project_id: str,
    template_key: str,
    body: UpdateTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upsert a single email template."""
    valid_keys = set(DEFAULT_TEMPLATES.keys())
    if template_key not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Invalid template key. Must be one of: {valid_keys}")
    try:
        await db.execute(
            text("""
                INSERT INTO project_email_templates (id, project_id, template_key, subject, body, updated_at)
                VALUES (:id, :project_id, :template_key, :subject, :body, NOW())
                ON CONFLICT (project_id, template_key) DO UPDATE
                  SET subject = EXCLUDED.subject,
                      body = EXCLUDED.body,
                      updated_at = NOW()
            """),
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "template_key": template_key,
                "subject": body.subject,
                "body": body.body,
            },
        )
        await db.commit()
        return {"data": {"saved": True, "template_key": template_key}}
    except Exception as e:
        logger.error("Failed to save template %s: %s", template_key, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/auth/templates/{template_key}/test", dependencies=[InternalGuard])
async def send_test_email(
    project_id: str,
    template_key: str,
    to_email: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Send a test email using the project's SMTP settings."""
    valid_keys = set(DEFAULT_TEMPLATES.keys())
    if template_key not in valid_keys:
        raise HTTPException(status_code=400, detail="Invalid template key")

    # Get SMTP settings
    smtp_cfg: dict = {}
    try:
        result = await db.execute(
            text("SELECT settings_json FROM project_auth_settings WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        row = result.mappings().first()
        if row:
            loaded = json.loads(row["settings_json"])
            smtp_cfg = loaded.get("smtp", {})
    except Exception:
        pass

    # Get template
    templates_result = await db.execute(
        text("""
            SELECT subject, body FROM project_email_templates
            WHERE project_id = :project_id AND template_key = :key
        """),
        {"project_id": project_id, "key": template_key},
    )
    tmpl_row = templates_result.mappings().first()
    template = (
        {"subject": tmpl_row["subject"], "body": tmpl_row["body"]}
        if tmpl_row
        else DEFAULT_TEMPLATES[template_key]
    )

    # Fill placeholder values for preview
    subject = template["subject"]
    body_text = template["body"].replace("{{name}}", "Test User")
    body_text = body_text.replace("{{verification_url}}", "https://example.com/verify?token=test123")
    body_text = body_text.replace("{{reset_url}}", "https://example.com/reset?token=test123")
    body_text = body_text.replace("{{confirmation_url}}", "https://example.com/confirm?token=test123")
    body_text = body_text.replace("{{magic_url}}", "https://example.com/magic?token=test123")
    body_text = body_text.replace("{{new_email}}", to_email)
    body_text = body_text.replace("{{app_name}}", "YourBaaS")

    smtp_host = smtp_cfg.get("host") or settings.smtp_host
    smtp_port = smtp_cfg.get("port") or settings.smtp_port
    smtp_user = smtp_cfg.get("user") or settings.smtp_user
    smtp_pass = smtp_cfg.get("password") or settings.smtp_pass
    smtp_secure = smtp_cfg.get("secure", settings.smtp_secure)
    from_email = smtp_cfg.get("from_email") or smtp_user or "noreply@yourbaas.com"
    from_name = smtp_cfg.get("from_name") or "YourBaaS"

    if not smtp_host:
        raise HTTPException(status_code=400, detail="SMTP not configured. Set SMTP host in email settings first.")

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[TEST] {subject}"
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            if smtp_secure:
                server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, [to_email], msg.as_string())
        return {"data": {"sent": True, "to": to_email}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")


# ─── JWT Rotation ─────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/auth/rotate-jwt", dependencies=[InternalGuard])
async def rotate_jwt_secret(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Rotate the per-project JWT secret — invalidates all existing tokens."""
    import secrets as sec
    new_secret = sec.token_urlsafe(32)
    result = await db.execute(
        text("""
            UPDATE projects SET auth_jwt_secret = :secret WHERE id = :project_id
            RETURNING id
        """),
        {"secret": new_secret, "project_id": project_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()
    logger.info("Rotated JWT secret for project: %s", project_id)
    return {"data": {"rotated": True, "project_id": project_id}}