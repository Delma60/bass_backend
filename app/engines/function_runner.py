import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Functions are stored in DB and executed via a sandbox
# For now we support HTTP-based edge functions


async def invoke_function(
    project_id: str,
    function_name: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Invoke a named edge function for a project.
    Functions are looked up from DB by name and project_id.
    """
    from sqlalchemy import text
    from app.db.postgres import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT endpoint_url, method, timeout_ms, is_active
                FROM edge_functions
                WHERE project_id = :project_id AND name = :name
                """
            ),
            {"project_id": project_id, "name": function_name},
        )
        fn = result.mappings().first()

    if not fn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Function '{function_name}' not found")

    if not fn["is_active"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Function is inactive")

    timeout_ms = int(fn["timeout_ms"] or 10000)
    timeout_ms = max(1000, min(timeout_ms, 15000))
    timeout_s = timeout_ms / 1000
    method = (fn["method"] or "POST").upper()
    merged_headers = {"Content-Type": "application/json", **(headers or {})}

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0)) as client:
        try:
            response = await client.request(
                method,
                fn["endpoint_url"],
                json=payload,
                headers=merged_headers,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("Function invocation timed out: %s", exc)
            from fastapi import HTTPException
            raise HTTPException(status_code=504, detail="Function invocation timed out")
        except httpx.RequestError as exc:
            logger.error("Function invocation failed: %s", exc)
            from fastapi import HTTPException
            raise HTTPException(status_code=502, detail="Function invocation failed")

    return {
        "status": response.status_code,
        "data": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
        "headers": dict(response.headers),
    }