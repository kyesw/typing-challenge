"""Shared helpers for API-layer integration tests.

Builds a FastAPI app wired against a fresh in-memory SQLite engine so
each test gets an isolated database. The app is constructed *without*
running the real ``lifespan`` (so we don't hit the file-backed DB);
instead we set ``app.state`` manually with the in-memory plumbing
plus a seeded prompt row.

Intentionally minimal — the app under test is the same
:func:`app.main.create_app` the production path returns, with the
``state`` slots overwritten after the ``TestClient`` enters its
lifespan. ``TestClient`` as a context manager runs the real
lifespan, so we overwrite the state *after* entering.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.main import create_app
from app.persistence import Prompt, PromptRepository, init_db


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def make_memory_engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced.

    Uses ``StaticPool`` so every connection opened by the engine
    shares a single underlying SQLite database — without this, each
    new session would get its own empty in-memory DB.
    ``check_same_thread=False`` lets FastAPI's thread-pool session
    usage cross-thread under the TestClient.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine


def seed_prompt(session_factory: sessionmaker[Session], *, text: str | None = None) -> str:
    """Insert one prompt row and return its id."""
    prompt_id = str(uuid.uuid4())
    with session_factory() as s:
        s.add(
            Prompt(
                id=prompt_id,
                text=text if text is not None else "x" * 120,
                difficulty=None,
                language="en",
            )
        )
        s.commit()
    return prompt_id


@contextmanager
def build_test_app(
    *,
    settings: Settings | None = None,
    prompt_text: str | None = None,
    random_choice: Callable[[list[str]], str] | None = None,
) -> Iterator[tuple[FastAPI, TestClient, sessionmaker[Session]]]:
    """Yield ``(app, client, session_factory)`` wired to an in-memory DB.

    The :class:`TestClient` is used as a context manager so FastAPI's
    lifespan runs — but the real lifespan's persistence fails loudly
    against ``:memory:`` unless we inject our own state after. We do
    exactly that: enter the lifespan, overwrite ``app.state``, run
    the test, exit.

    Args:
        settings: Optional custom settings. Defaults to the module's
            cached settings; tests that need different TTLs can pass
            their own.
        prompt_text: Optional prompt text. If given, seeds a single
            prompt with this text; otherwise a 120-char placeholder.
        random_choice: Optional override for the PromptRepository's
            random pick. Used only when the test cares which prompt
            was returned — the default uniform pick is fine for the
            single-seeded-row case.
    """
    settings = settings if settings is not None else get_settings()

    engine = make_memory_engine()
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    app = create_app(settings)
    # Override the settings dependency so anywhere that reads
    # ``settings_dependency`` sees the same instance this test is
    # configured with.
    from app.api.dependencies import settings_dependency

    app.dependency_overrides[settings_dependency] = lambda: settings

    with TestClient(app) as client:
        # Swap in our in-memory wiring AFTER the real lifespan
        # finishes. The real lifespan may have failed against
        # ``sqlite:///./typing_game.db``; we don't care — we replace
        # its state unconditionally.
        app.state.db_engine = engine
        app.state.session_factory = session_factory
        repo_kwargs = {}
        if random_choice is not None:
            repo_kwargs["random_choice"] = random_choice
        app.state.prompt_repository = PromptRepository(
            session_factory, **repo_kwargs
        )

        # Seed one prompt so GameService.create_game always finds one.
        seed_prompt(session_factory, text=prompt_text)

        yield app, client, session_factory

    app.dependency_overrides.clear()


def register_player(client: TestClient, nickname: str = "Alice") -> dict:
    """Helper: register a player and return the response JSON."""
    response = client.post("/players", json={"nickname": nickname})
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(token: str) -> dict[str, str]:
    """Build the ``Authorization: Bearer ...`` header dict."""
    return {"Authorization": f"Bearer {token}"}


__all__ = [
    "auth_headers",
    "build_test_app",
    "make_memory_engine",
    "register_player",
    "seed_prompt",
]
