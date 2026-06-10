# backend/app/api/v1/functions/router.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import AuthCtx, ProjectCtx, require_key_type
from app.engines.function_runner import invoke_function
from app.models.requests import FunctionInvokeRequest
from app.tasks.usage_sync import record_usage
router = APIRouter(prefix="/functions", tags=["Edge Functions"])
logger = logging.getLogger(__name__)


@router.post("/{project_id}/invoke/{function_name}")
async def invoke(
    project_id: str,
    function_name: str,
    body: FunctionInvokeRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    if not function_name.isidentifier():
        raise HTTPException(status_code=400, detail="Invalid function name")

    extra_headers = dict(body.headers)
    if auth.is_authenticated and auth.uid:
        extra_headers["X-User-Id"] = auth.uid
        extra_headers["X-User-Email"] = auth.email or ""

    result = await invoke_function(
        project_id=project_id,
        function_name=function_name,
        payload=body.payload,
        headers=extra_headers,
    )
    record_usage.delay(project_id, "function_calls", 1)
    return {"data": result}