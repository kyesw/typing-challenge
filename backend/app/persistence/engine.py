"""SQLAlchemy engine and session factories driven by ``Settings.database_url``.

The v1 deployment target is SQLite on a single lounge host, so we use
``check_same_thread=False`` for SQLite URLs to allow the FastAPI worker
to share the engine across request threads. Tests construct their own
in-memory engines directly and do not need to go through here.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings


def create_engine_from_settings(settings: Settings | None = None) -> Engine:
    """Build a SQLAlchemy engine from the supplied (or cached) settings."""
    settings = settings or get_settings()
    url = settings.database_url

    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # FastAPI may access the session from a threadpool worker; SQLite's
        # default check blocks cross-thread reuse of a single connection.
        connect_args["check_same_thread"] = False

    return create_engine(url, connect_args=connect_args, future=True)


def get_sessionmaker(
    engine: Engine | None = None,
    settings: Settings | None = None,
) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine`` (or one built from settings)."""
    bound = engine if engine is not None else create_engine_from_settings(settings)
    return sessionmaker(bind=bound, autoflush=False, expire_on_commit=False, future=True)


__all__ = ["create_engine_from_settings", "get_sessionmaker"]
