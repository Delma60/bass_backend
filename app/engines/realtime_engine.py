# backend/app/engines/realtime_engine.py
"""
Realtime engine — manages PostgreSQL LISTEN/NOTIFY listeners and
MongoDB Change Stream watchers. Results are broadcast to Socket.io
clients via Redis pub/sub so any worker node can forward events.
"""
import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def listen_postgres_channel(
    channel: str,
    redis_publish_key: str,
) -> None:
    """
    Open a raw asyncpg connection (not SQLAlchemy) and LISTEN on
    a PostgreSQL NOTIFY channel. Each notification is published to
    the given Redis key for Socket.io workers to pick up.
    """
    import asyncpg
    from app.config import settings

    # asyncpg needs a plain DSN (no +asyncpg driver prefix)
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _handler(conn, pid, channel_name, payload):  # type: ignore[no-untyped-def]
        try:
            data = json.loads(payload)
            from app.db.redis import get_redis
            redis = await get_redis()
            await redis.publish(redis_publish_key, json.dumps(data))
            logger.debug("Published realtime event on %s", channel_name)
        except Exception as e:
            logger.error("Realtime handler error on %s: %s", channel_name, e)

    try:
        conn = await asyncpg.connect(dsn)
        await conn.add_listener(channel, _handler)
        logger.info("Listening on PostgreSQL channel: %s", channel)
        # Keep alive — caller manages lifecycle
        while True:
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        logger.info("Stopped listening on channel: %s", channel)
        raise
    except Exception as e:
        logger.error("Failed to listen on channel %s: %s", channel, e)
        raise


async def get_channel_name(schema: str, table_or_collection: str) -> str:
    """Return the canonical notify channel name for a resource."""
    return f"{schema}_{table_or_collection}_changes"


async def broadcast_event(
    project_id: str,
    resource: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """
    Publish a realtime event to Redis for Socket.io to forward.
    Used by insert/update/delete engines when pg_notify is not available.
    """
    from app.db.redis import get_redis
    redis = await get_redis()
    message = json.dumps({
        "project_id": project_id,
        "resource": resource,
        "type": event_type,
        "payload": payload,
    })
    channel = f"realtime:{project_id}:{resource}"
    await redis.publish(channel, message)
    logger.debug("Broadcast %s event for %s/%s", event_type, project_id, resource)