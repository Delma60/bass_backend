# backend/app/provisioner/sql_provisioner.py
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import AsyncSessionLocal

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_id(name: str, label: str = "identifier") -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid {label}: {name!r}")
    return name


def _safe_on_delete(action: str) -> str:
    allowed = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}
    upper = action.upper()
    if upper not in allowed:
        raise ValueError(f"Invalid ON DELETE action: {action!r}")
    return upper


async def provision_project_schema(project_id: str, db_schema: str) -> None:
    if not db_schema.replace("_", "").isalnum() or not db_schema.startswith("proj_"):
        raise ValueError(f"Invalid schema name: {db_schema}")

    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{db_schema}"'))
            await session.execute(text(f"""
                CREATE TABLE IF NOT EXISTS "{db_schema}"."_auth_users" (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT,
                    hashed_password TEXT NOT NULL,
                    is_email_verified BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await session.execute(text(f"""
                CREATE OR REPLACE FUNCTION "{db_schema}".set_updated_at()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = NOW();
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
            """))
            await session.commit()
            logger.info("Provisioned SQL schema '%s' for project '%s'", db_schema, project_id)
        except Exception as e:
            await session.rollback()
            logger.error("Failed to provision SQL schema for %s: %s", project_id, str(e))
            raise


async def teardown_project_schema(schema_name: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await session.commit()
    logger.info("Torn down schema: %s", schema_name)


async def create_table(
    session: AsyncSession,
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
    *,
    enable_rls: bool = False,
    enable_realtime: bool = False,
) -> None:
    """
    Column shape:
      { "name": "user_id", "type": "text", "nullable": True, "default": None,
        "foreign_key": { "table": "users", "column": "id", "on_delete": "CASCADE" } }
    """
    _safe_id(schema, "schema")
    _safe_id(table, "table")

    col_defs = ['id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text']
    fk_constraints: list[str] = []

    for col in columns:
        col_name = _safe_id(col["name"], "column")
        col_type = _map_type(col.get("type", "text"))
        nullable = "NULL" if col.get("nullable", True) else "NOT NULL"
        default = f"DEFAULT {col['default']}" if col.get("default") is not None else ""
        col_defs.append(f'"{col_name}" {col_type} {nullable} {default}'.strip())

        fk = col.get("foreign_key")
        if fk:
            ref_table = _safe_id(fk["table"], "ref_table")
            ref_col = _safe_id(fk.get("column", "id"), "ref_column")
            on_delete = _safe_on_delete(fk.get("on_delete", "NO ACTION"))
            fk_name = f"fk_{table}_{col_name}"
            fk_constraints.append(
                f'CONSTRAINT "{fk_name}" FOREIGN KEY ("{col_name}") '
                f'REFERENCES "{schema}"."{ref_table}" ("{ref_col}") '
                f'ON DELETE {on_delete}'
            )

    col_defs.append("created_at TIMESTAMPTZ DEFAULT NOW()")
    col_defs.append("updated_at TIMESTAMPTZ DEFAULT NOW()")

    all_defs = col_defs + fk_constraints
    ddl = f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" ({", ".join(all_defs)})'
    await session.execute(text(ddl))

    await session.execute(text(f"""
        CREATE OR REPLACE FUNCTION "{schema}".set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))
    await session.execute(text(f'DROP TRIGGER IF EXISTS set_updated_at ON "{schema}"."{table}"'))
    await session.execute(text(f"""
        CREATE TRIGGER set_updated_at
        BEFORE UPDATE ON "{schema}"."{table}"
        FOR EACH ROW EXECUTE FUNCTION "{schema}".set_updated_at();
    """))

    if enable_realtime:
        await _create_realtime_trigger(session, schema, table)

    logger.info("Created table: %s.%s", schema, table)


async def drop_table(session: AsyncSession, schema: str, table: str) -> None:
    await session.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE'))
    logger.info("Dropped table: %s.%s", schema, table)


async def add_column(
    session: AsyncSession,
    schema: str,
    table: str,
    column: dict[str, Any],
) -> None:
    col_name = _safe_id(column["name"], "column")
    col_type = _map_type(column.get("type", "text"))
    nullable = "NULL" if column.get("nullable", True) else "NOT NULL"
    default = f"DEFAULT {column['default']}" if column.get("default") is not None else ""

    await session.execute(
        text(
            f'ALTER TABLE "{schema}"."{table}" '
            f'ADD COLUMN IF NOT EXISTS "{col_name}" {col_type} {nullable} {default}'
        )
    )

    fk = column.get("foreign_key")
    if fk:
        ref_table = _safe_id(fk["table"], "ref_table")
        ref_col = _safe_id(fk.get("column", "id"), "ref_column")
        on_delete = _safe_on_delete(fk.get("on_delete", "NO ACTION"))
        fk_name = f"fk_{table}_{col_name}"
        await session.execute(
            text(f'ALTER TABLE "{schema}"."{table}" DROP CONSTRAINT IF EXISTS "{fk_name}"')
        )
        await session.execute(
            text(
                f'ALTER TABLE "{schema}"."{table}" '
                f'ADD CONSTRAINT "{fk_name}" FOREIGN KEY ("{col_name}") '
                f'REFERENCES "{schema}"."{ref_table}" ("{ref_col}") '
                f'ON DELETE {on_delete}'
            )
        )


async def drop_column(session: AsyncSession, schema: str, table: str, column: str) -> None:
    await session.execute(
        text(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN IF EXISTS "{column}"')
    )


async def get_foreign_keys(session: AsyncSession, schema: str) -> list[dict[str, Any]]:
    """Return all FK relationships in the project schema for ERD rendering."""
    result = await session.execute(
        text("""
            SELECT
                tc.table_name        AS from_table,
                kcu.column_name      AS from_column,
                ccu.table_name       AS to_table,
                ccu.column_name      AS to_column,
                rc.delete_rule       AS on_delete,
                tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
               AND tc.table_schema   = kcu.table_schema
            JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON rc.unique_constraint_name = ccu.constraint_name
               AND ccu.table_schema          = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema    = :schema
            ORDER BY tc.table_name, kcu.column_name
        """),
        {"schema": schema},
    )
    return [dict(r) for r in result.mappings()]


async def _create_realtime_trigger(session: AsyncSession, schema: str, table: str) -> None:
    channel = f"{schema}_{table}_changes"
    fn_name = f"{schema}_{table}_notify"

    await session.execute(text(f"""
        CREATE OR REPLACE FUNCTION "{fn_name}"() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            PERFORM pg_notify('{channel}', json_build_object(
              'type', TG_OP, 'old', row_to_json(OLD)
            )::text);
          ELSE
            PERFORM pg_notify('{channel}', json_build_object(
              'type', TG_OP, 'new', row_to_json(NEW)
            )::text);
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))
    await session.execute(text(f'DROP TRIGGER IF EXISTS "{fn_name}_trigger" ON "{schema}"."{table}"'))
    await session.execute(text(f"""
        CREATE TRIGGER "{fn_name}_trigger"
        AFTER INSERT OR UPDATE OR DELETE ON "{schema}"."{table}"
        FOR EACH ROW EXECUTE FUNCTION "{fn_name}"()
    """))


def _map_type(t: str) -> str:
    type_map = {
        "text": "TEXT",
        "string": "TEXT",
        "int": "INTEGER",
        "integer": "INTEGER",
        "float": "DOUBLE PRECISION",
        "double": "DOUBLE PRECISION",
        "bool": "BOOLEAN",
        "boolean": "BOOLEAN",
        "json": "JSONB",
        "jsonb": "JSONB",
        "timestamp": "TIMESTAMPTZ",
        "date": "DATE",
        "uuid": "UUID",
        "vector": "vector",  # pgvector
    }
    return type_map.get(t.lower(), "TEXT")