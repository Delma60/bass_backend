from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

_client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]


def get_mongo_client() -> AsyncIOMotorClient:  # type: ignore[type-arg]
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongodb_url)
    return _client


def get_platform_db() -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    return get_mongo_client()[settings.mongodb_db_name]


def get_project_db(mongo_database: str) -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    """Return the MongoDB database for a specific project."""
    return get_mongo_client()[mongo_database]


async def close_mongo_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None