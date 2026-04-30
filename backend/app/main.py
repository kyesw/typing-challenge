"""FastAPI application factory and app instance.

The lifespan hook builds the SQLAlchemy engine + session factory and
seeds the prompt table on first boot. It also wires the shared
singletons that per-request dependencies in :mod:`app.api.dependencies`
read back off ``app.state``:

* ``app.state.db_engine`` — SQLAlchemy engine (task 2.1)
* ``app.state.session_factory`` — session factory bound to the engine
* ``app.state.prompt_repository`` — :class:`PromptRepository`
  (task 2.4 / 11.1)

Schema creation and prompt seeding are wrapped in a broad
``try/except`` so a transient persistence failure does not take
``/health`` offline. If startup fails every field is left ``None``
and the dependency accessors surface 503 with the shared error
envelope, which is the right user-facing signal.

Later tasks will wire the background timeout sweeper (task 4.5) into
the same lifespan.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api import games_router, leaderboard_router, players_router
from .api.rate_limit import TokenBucketLimiter, per_minute_to_per_sec
from .config import Settings, get_settings
from .errors import install_error_handlers
from .persistence import (
    PromptRepository,
    create_engine_from_settings,
    get_sessionmaker,
    init_db,
    seed_prompts_if_empty,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI instance with the shared error-response contract wired in."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Best-effort schema creation + prompt seeding. Store the
        # engine on app.state so request dependencies can reuse it.
        # Any exception here is logged and swallowed so the /health
        # liveness probe stays up even if persistence is momentarily
        # unavailable.
        app.state.db_engine = None
        app.state.session_factory = None
        app.state.prompt_repository = None
        try:
            engine = create_engine_from_settings(settings)
            init_db(engine)
            inserted = seed_prompts_if_empty(engine)
            if inserted:
                logger.info("Seeded %d prompt(s) on startup.", inserted)
            else:
                logger.info("Prompt table already populated; skipping seed.")
            session_factory = get_sessionmaker(engine, settings)
            app.state.db_engine = engine
            app.state.session_factory = session_factory
            app.state.prompt_repository = PromptRepository(session_factory)
        except Exception:  # pragma: no cover - defensive startup guard
            logger.exception(
                "Startup persistence setup failed; continuing so /health stays available."
            )
        yield

    app = FastAPI(
        title="Typing Game API",
        version="0.1.0",
        description="Backend API for the lounge-style Typing Game.",
        lifespan=lifespan,
    )

    # Rate limiters (task 9.1 / Requirements 14.1, 14.2, 14.3).
    # Built once per app instance and attached to ``app.state`` so
    # dependencies can read them back. Tests rebuild the app per
    # case, so each test gets fresh, empty buckets.
    app.state.players_ip_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_players_per_ip_per_minute,
        refill_per_sec=per_minute_to_per_sec(
            settings.rate_limit_players_per_ip_per_minute
        ),
    )
    app.state.games_ip_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_games_per_ip_per_minute,
        refill_per_sec=per_minute_to_per_sec(
            settings.rate_limit_games_per_ip_per_minute
        ),
    )
    app.state.games_player_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_games_per_player_per_minute,
        refill_per_sec=per_minute_to_per_sec(
            settings.rate_limit_games_per_player_per_minute
        ),
    )

    install_error_handlers(app)

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        """Liveness / readiness probe.

        Checks DB connectivity via a lightweight ``SELECT 1``.  Returns
        200 when the database is reachable, 503 otherwise so the
        Kubernetes readiness probe can stop routing traffic to this pod.
        """
        db_ok = getattr(app.state, "session_factory", None) is not None
        if db_ok:
            try:
                with app.state.session_factory() as session:
                    session.execute(text("SELECT 1"))
            except Exception:
                db_ok = False
        if not db_ok:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "environment": settings.environment},
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "environment": settings.environment},
        )

    # Routers are included in the order of their resource prefixes.
    # All routes use the shared error-response contract installed above.
    app.include_router(players_router)
    app.include_router(games_router)
    app.include_router(leaderboard_router)

    return app


app = create_app()
