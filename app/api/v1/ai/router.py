# backend/app/api/v1/ai/router.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.dependencies import AuthCtx, ProjectCtx, require_key_type
from app.engines.vector_engine import similarity_search, upsert_embedding
from app.tasks.usage_sync import record_usage

router = APIRouter(prefix="/ai", tags=["AI / Vector"])
logger = logging.getLogger(__name__)


class EmbedRequest(BaseModel):
    text: str
    model: str = "text-embedding-3-small"


class UpsertEmbeddingRequest(BaseModel):
    id: str
    embedding: list[float]
    metadata: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    embedding: list[float] = Field(description="Query embedding vector")
    top_k: int = Field(default=10, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    filter: str | None = Field(default=None, description="Extra SQL WHERE condition")


@router.post("/{project_id}/embed")
async def create_embedding(
    project_id: str,
    body: EmbedRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
) -> dict[str, Any]:
    """Generate an embedding for the given text using OpenAI."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    from app.config import settings
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="AI service not configured")

    try:
        import httpx
        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"input": body.text, "model": body.model},
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data["data"][0]["embedding"]
    except Exception as e:
        logger.error("Embedding generation failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to generate embedding")

    record_usage.delay(project_id, "ai_requests", 1)
    return {"data": {"embedding": embedding, "model": body.model, "dimensions": len(embedding)}}


@router.post("/{project_id}/vectors/{table}/upsert", status_code=201)
async def upsert_vector(
    project_id: str,
    table: str,
    body: UpsertEmbeddingRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Insert or update an embedding record in a pgvector table."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await upsert_embedding(
        db,
        ctx["db_schema"],
        table,
        record_id=body.id,
        embedding=body.embedding,
        metadata=body.metadata,
    )
    await db.commit()
    record_usage.delay(project_id, "ai_requests", 1)
    return {"data": {"id": body.id, "upserted": True}}


@router.post("/{project_id}/vectors/{table}/search")
async def search_vectors(
    project_id: str,
    table: str,
    body: SearchRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cosine similarity search in a pgvector table."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    results = await similarity_search(
        db,
        ctx["db_schema"],
        table,
        embedding=body.embedding,
        top_k=body.top_k,
        threshold=body.threshold,
        extra_filter=body.filter,
    )
    return {"data": results, "meta": {"count": len(results)}}