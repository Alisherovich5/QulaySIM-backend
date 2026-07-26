"""Synchronous session for Celery tasks.

Celery's prefork pool is not an asyncio context; using the sync engine here is
simpler and safer than driving an event loop per task.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

sync_engine = create_engine(
    settings.sync_database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=settings.db_pool_recycle_seconds,
)

SyncSessionFactory = sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False)


@contextmanager
def worker_session() -> Iterator[Session]:
    session = SyncSessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
