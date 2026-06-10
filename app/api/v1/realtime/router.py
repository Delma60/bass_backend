# backend/app/api/v1/realtime/router.py
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.dependencies import AuthCtx, ProjectCtx
from app.models.requests import RealtimeSubscribeRequest
from app.tasks.usage_sync import record_usage

router = APIRouter(prefix="/realtime", tags=["Realtime"])
logger = logging.getLogger(__name__)


@router.get("/{project_id}/channels")
async def list_channels(
    project_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    """List all available realtime channels for this project."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    schema = ctx["db_schema"]
    mongo_db = ctx["mongo_database"]
    record_usage.delay(project_id, "db_reads", 1)

    return {
        "data": {
            "sql_channel_prefix": f"{schema}_",
            "nosql_channel_prefix": f"{mongo_db}_",
            "connection_info": {
                "type": "socket.io",
                "namespace": f"/{project_id}",
                "auth": "X-User-Token header required for private channels",
            },
        }
    }


@router.post("/{project_id}/subscribe")
async def get_subscription_token(
    project_id: str,
    body: RealtimeSubscribeRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    """
    Return channel name and connection info for a realtime subscription.
    The actual socket connection is handled by the Socket.io server.
    """
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    schema = ctx["db_schema"]
    resource = body.table_or_collection
    channel = f"{schema}_{resource}_changes"
    record_usage.delay(project_id, "db_reads", 1)

    return {
        "data": {
            "channel": channel,
            "resource": resource,
            "event_types": body.event_types,
            "connection_url": f"/realtime/{project_id}",
        }
    }