import asyncpg
from typing import Optional, Any

_POOL: Optional[asyncpg.Pool] = None

async def init_pool(database_url: str) -> None:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)

async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None

async def ensure_schema(schema_sql: str) -> None:
    if _POOL is None:
        raise RuntimeError("DB pool not initialized")
    async with _POOL.acquire() as conn:
        await conn.execute(schema_sql)

async def insert_message(
    session_id: str,
    call_sid: Optional[str],
    stream_sid: Optional[str],
    app_instance: str,
    role: str,
    content: str,
    ts_epoch: float,
) -> None:
    if _POOL is None:
        return
    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO transcript_messages
              (session_id, call_sid, stream_sid, app_instance, role, content, ts_epoch)
            VALUES
              ($1, $2, $3, $4, $5, $6, $7)
            """,
            session_id, call_sid, stream_sid, app_instance, role, content, ts_epoch,
        )

async def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    if _POOL is None:
        return []
    async with _POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT session_id,
                   MIN(created_at) AS started_at,
                   MAX(created_at) AS last_at,
                   COUNT(*)         AS message_count
            FROM transcript_messages
            GROUP BY session_id
            ORDER BY last_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]

async def fetch_session(session_id: str) -> list[dict[str, Any]]:
    if _POOL is None:
        return []
    async with _POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT session_id, call_sid, stream_sid, app_instance, role, content, ts_epoch, created_at
            FROM transcript_messages
            WHERE session_id = $1
            ORDER BY ts_epoch ASC
            """,
            session_id,
        )
    return [dict(r) for r in rows]
