# backend/app/engines/vector_engine.py
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import set_tenant_session

logger = logging.getLogger(__name__)

# Only allow simple column comparisons in extra_filter: e.g. "col = 'val'" or "col IS NULL"
# This prevents arbitrary SQL injection through the extra_filter parameter.
_SAFE_FILTER_RE = re.compile(
    r"""^[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=|!=|<|>|<=|>=|IS\s+(?:NOT\s+)?NULL|ILIKE|LIKE)\s*(?:'[^']*'|\d+(?:\.\d+)?|NULL|TRUE|FALSE)$""",
    re.IGNORECASE,
)


def _validate_extra_filter(extra_filter: str) -> None:
    """
    Whitelist-validate extra_filter to prevent SQL injection.
    Only simple col OP literal expressions are permitted.
    Raises ValueError if the filter does not match the safe pattern.
    """
    if not _SAFE_FILTER_RE.match(extra_filter.strip()):
        raise ValueError(
            f"extra_filter contains disallowed SQL. "
            f"Only simple column comparisons are permitted. Got: {extra_filter!r}"
        )


def _validate_identifier(name: str) -> None:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        raise ValueError(f"Invalid identifier: {name!r}")


def _embedding_to_pg_literal(embedding: list[float]) -> str:
    """Safely convert a float list to a PostgreSQL vector literal."""
    if not embedding:
        raise ValueError("Embedding must be non-empty")
    # Each element must be a real float — reject anything else
    validated = [float(x) for x in embedding]
    return "[" + ",".join(repr(v) for v in validated) + "]"


async def similarity_search(
    session: AsyncSession,
    schema: str,
    table: str,
    embedding: list[float],
    *,
    top_k: int = 10,
    threshold: float | None = None,
    extra_filter: str | None = None,
) -> list[dict[str, Any]]:
    """
    Perform cosine similarity search using pgvector.
    The table must have an 'embedding' column of type vector.
    """
    _validate_identifier(schema)
    _validate_identifier(table)
    await set_tenant_session(session, schema)

    vector_literal = _embedding_to_pg_literal(embedding)

    where_parts = ["TRUE"]

    if threshold is not None:
        if not (0.0 <= threshold <= 1.0):
            raise ValueError("threshold must be between 0.0 and 1.0")
        # threshold is a validated Python float — safe to interpolate
        where_parts.append(
            f"1 - (embedding <=> '{vector_literal}'::vector) >= {float(threshold)!r}"
        )

    if extra_filter:
        _validate_extra_filter(extra_filter)
        where_parts.append(f"({extra_filter})")

    where = " AND ".join(where_parts)

    # vector_literal is constructed entirely from validated floats — not user strings
    query = f"""
        SELECT *, 1 - (embedding <=> '{vector_literal}'::vector) AS similarity
        FROM "{schema}"."{table}"
        WHERE {where}
        ORDER BY embedding <=> '{vector_literal}'::vector
        LIMIT :top_k
    """

    result = await session.execute(text(query), {"top_k": min(top_k, 100)})
    return [dict(r._mapping) for r in result]


async def upsert_embedding(
    session: AsyncSession,
    schema: str,
    table: str,
    record_id: Any,
    embedding: list[float],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert or update an embedding record."""
    _validate_identifier(schema)
    _validate_identifier(table)
    await set_tenant_session(session, schema)

    vector_literal = _embedding_to_pg_literal(embedding)

    meta_cols = ""
    meta_vals = ""
    meta_update = ""
    params: dict[str, Any] = {"record_id": record_id, "embedding": vector_literal}

    if metadata:
        for k, v in metadata.items():
            # Validate metadata column names
            _validate_identifier(k)
            col = k
            meta_cols += f', "{col}"'
            meta_vals += f", :{col}"
            meta_update += f', "{col}" = EXCLUDED."{col}"'
            params[col] = v

    query = f"""
        INSERT INTO "{schema}"."{table}" (id, embedding{meta_cols})
        VALUES (:record_id, :embedding::vector{meta_vals})
        ON CONFLICT (id) DO UPDATE
        SET embedding = EXCLUDED.embedding{meta_update}
    """
    await session.execute(text(query), params)