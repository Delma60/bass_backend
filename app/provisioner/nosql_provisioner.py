import logging

from app.db.mongo import get_project_db
from pymongo import ASCENDING, IndexModel

logger = logging.getLogger(__name__)

KV_COLLECTION = "_kv"


async def provision_project_database(project_id: str, mongo_database: str) -> None:
    """Sets up the MongoDB database and required indexes for the project."""
    
    if not mongo_database.replace("_", "").isalnum() or not mongo_database.startswith("proj_"):
        raise ValueError(f"Invalid database name: {mongo_database}")

    db = get_project_db(mongo_database)
    kv_coll = db[KV_COLLECTION]
    
    try:
        # 1. Unique index on 'key' to prevent duplicate keys
        # 2. TTL index on 'expires_at' (MongoDB automatically deletes expired docs)
        indexes = [
            IndexModel([("key", ASCENDING)], unique=True),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0)
        ]
        
        await kv_coll.create_indexes(indexes)
        logger.info("✅ Provisioned MongoDB database '%s' for project '%s'", mongo_database, project_id)
    except Exception as e:
        logger.error("❌ Failed to provision NoSQL database for %s: %s", project_id, str(e))
        raise
    
async def teardown_project_database(mongo_database: str) -> None:
    """Drop a project's MongoDB database entirely."""
    from app.db.mongo import get_mongo_client
    client = get_mongo_client()
    await client.drop_database(mongo_database)
    logger.info("Torn down MongoDB database: %s", mongo_database)


async def create_collection(
    mongo_database: str,
    collection: str,
    *,
    indexes: list[dict] | None = None,
    enable_change_stream: bool = False,
) -> None:
    """Create a MongoDB collection and optionally add indexes."""
    if collection.startswith("_"):
        raise ValueError(f"Collection names starting with '_' are reserved: {collection}")

    db = get_project_db(mongo_database)
    coll = db[collection]

    # Create collection explicitly (insert + delete a dummy doc)
    # This ensures the collection exists before any queries
    try:
        await db.create_collection(collection)
    except Exception:
        pass  # Already exists

    # Created_at index for sorting
    await coll.create_index("_id")

    if indexes:
        for idx in indexes:
            keys = [(k, v) for k, v in idx.get("keys", {}).items()]
            options = {k: v for k, v in idx.items() if k != "keys"}
            if keys:
                await coll.create_index(keys, **options)

    logger.info("Created collection: %s.%s", mongo_database, collection)


async def drop_collection(mongo_database: str, collection: str) -> None:
    if collection.startswith("_"):
        raise ValueError(f"Cannot drop reserved collection: {collection}")

    db = get_project_db(mongo_database)
    await db.drop_collection(collection)
    logger.info("Dropped collection: %s.%s", mongo_database, collection)