"""
Database layer — SQLAlchemy engine + session for PostgreSQL (Neon).

Main multi-tenant store. Uses Postgres in production (DATABASE_URL=postgresql://...)
and falls back to a local SQLite file for quick dev tests when no Postgres is set.

DuckDB stays available separately for heavy analytics (data/analytics.py).
"""

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import settings

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    # Neon/Heroku sometimes hand out 'postgres://'; SQLAlchemy wants 'postgresql://'
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Use psycopg2 driver explicitly
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


DATABASE_URL = _normalize_url(settings.database_url)
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

# Engine config differs by backend
if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    # SQLite needs FK enforcement turned on explicitly
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_con, _):
        dbapi_con.execute("PRAGMA foreign_keys=ON")
else:
    # Neon: serverless Postgres. pool_pre_ping avoids stale-connection errors after idle.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=300,
        connect_args={"sslmode": "require"} if "neon.tech" in DATABASE_URL or "sslmode" not in DATABASE_URL else {},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Session:
    """FastAPI dependency — yields a session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Session:
    """Context-manager session for schedulers, bots, scripts (commits on success)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create all tables (used by dev / first run). Production uses Alembic migrations."""
    from app.models.base import Base
    import app.models.tenant  # noqa: register models
    import app.models.domain  # noqa
    import app.models.future  # noqa
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    logger.info(f"DB schema ensured on {'SQLite' if IS_SQLITE else 'PostgreSQL'}")


def _ensure_columns():
    """Lightweight idempotent column adds for SQLite dev DBs (create_all won't ALTER
    existing tables). Production uses Alembic; this is a safe no-op there."""
    if not IS_SQLITE:
        return
    from sqlalchemy import text
    add = [("ingredient_price_history", "category", "VARCHAR(20)"),
           ("chat_messages", "thread_id", "VARCHAR(36)")]
    added = set()
    with engine.begin() as conn:
        for table, col, coltype in add:
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
            if col not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                logger.info(f"DB migrate: added {table}.{col}")
                added.add((table, col))
    if ("chat_messages", "thread_id") in added:
        _backfill_chat_threads()


def _backfill_chat_threads(gap_hours: int = 12):
    """Assign thread_ids to pre-existing chat rows so old conversations still show
    up in history (grouped by >gap_hours idle gaps), rather than vanishing."""
    import uuid
    from datetime import timedelta
    from sqlalchemy import text
    gap = timedelta(hours=gap_hours)
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, tenant_id, session, created_at FROM chat_messages "
            "WHERE thread_id IS NULL ORDER BY tenant_id, session, created_at ASC"
        )).fetchall()
        from datetime import datetime as _dt
        prev_key, prev_at, tid = None, None, None
        for rid, tenant_id, session, created_at in rows:
            key = (tenant_id, session)
            at = created_at if isinstance(created_at, _dt) else _dt.fromisoformat(str(created_at))
            if key != prev_key or prev_at is None or (at - prev_at) > gap:
                tid = uuid.uuid4().hex[:32]
            conn.execute(text("UPDATE chat_messages SET thread_id = :t WHERE id = :i"),
                         {"t": tid, "i": rid})
            prev_key, prev_at = key, at
    logger.info(f"DB migrate: backfilled chat_messages.thread_id ({len(rows)} rows)")
