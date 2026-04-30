"""SQLAlchemy declarative base for all persistence models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base so all ORM models share one ``MetaData``."""


__all__ = ["Base"]
