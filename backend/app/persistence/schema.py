"""Schema creation helper.

v1 treats "migrations" as programmatic ``Base.metadata.create_all``
against SQLite. When the project grows a second instance or a real
RDBMS, swap this for Alembic; the call site (FastAPI startup) will not
need to change.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

# Import models so their tables are registered on ``Base.metadata``
# before ``create_all`` runs. The ``noqa`` silences an F401 that would
# otherwise flag the imports as unused.
from . import models  # noqa: F401
from .base import Base


def init_db(engine: Engine) -> None:
    """Create all registered tables on ``engine`` if they do not exist.

    Safe to call multiple times; ``create_all`` is a no-op on tables that
    already exist. Tests pass an in-memory SQLite engine here; the
    FastAPI startup path passes the app-wide engine.
    """
    Base.metadata.create_all(engine)


__all__ = ["init_db"]
