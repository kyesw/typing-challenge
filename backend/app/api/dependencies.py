"""Shared FastAPI dependencies for the HTTP API.

The HTTP layer is stateless at the request level: every endpoint asks
the dependency injector for the services it needs, which in turn pull
shared infrastructure (DB engine, session factory, event publisher)
from :attr:`FastAPI.state` set up by the app's lifespan hook in
:mod:`app.main`.

This keeps the routers free of global-state lookups, keeps the app's
test harness simple (``app.dependency_overrides`` is enough to swap
any layer), and matches the service-layer convention of "session
factory in, no hidden singletons".

Requirements addressed:
- 7.2, 7.3 (``require_player`` — Session_Token authorization)
- 11.1 (Prompt_Repository selection policy injected into GameService)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings
from ..errors import ErrorCode, RateLimited, Unauthorized as UnauthorizedApiError
from ..persistence import PromptRepository
from ..services import (
    AuthorizedPlayer,
    GameService,
    LeaderboardService,
    PlayerService,
    ScoringService,
    Unauthorized,
)
from .rate_limit import TokenBucketLimiter


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings_dependency() -> Settings:
    """Return the cached :class:`Settings`.

    Wrapped in a FastAPI dependency so tests can override it via
    ``app.dependency_overrides[settings_dependency]``.
    """
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dependency)]


# ---------------------------------------------------------------------------
# Infrastructure accessors
# ---------------------------------------------------------------------------


def get_engine(request: Request) -> Engine:
    """Return the SQLAlchemy engine stashed on ``app.state`` at startup.

    The app's lifespan hook in :func:`app.main.create_app` builds the
    engine (``app.state.db_engine``). If startup failed (e.g., the DB
    URL is unreachable) ``db_engine`` will be ``None``; we surface a
    503 in that case rather than a confusing AttributeError deeper
    in the stack.
    """
    engine: Engine | None = getattr(request.app.state, "db_engine", None)
    if engine is None:
        # 503 is the right shape for "service started but persistence
        # is not available"; the health probe still reports ``ok``
        # because it does not touch the DB.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not available.",
        )
    return engine


EngineDep = Annotated[Engine, Depends(get_engine)]


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Return the session factory stashed on ``app.state`` at startup."""
    factory: sessionmaker[Session] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not available.",
        )
    return factory


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]


def get_prompt_repository(request: Request) -> PromptRepository:
    """Return the prompt repository stashed on ``app.state``.

    The repository is stateless across requests (it constructs a new
    session per call), so a single instance can be reused.
    """
    repo: PromptRepository | None = getattr(
        request.app.state, "prompt_repository", None
    )
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prompt repository is not available.",
        )
    return repo


PromptRepositoryDep = Annotated[PromptRepository, Depends(get_prompt_repository)]


# ---------------------------------------------------------------------------
# Service factories
# ---------------------------------------------------------------------------


def get_player_service(
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
) -> PlayerService:
    """Build a :class:`PlayerService` scoped to this request.

    The service itself is stateless apart from the injected factories,
    so constructing one per request is cheap.
    """
    return PlayerService(session_factory, settings=settings)


PlayerServiceDep = Annotated[PlayerService, Depends(get_player_service)]


def get_scoring_service() -> ScoringService:
    """Build a :class:`ScoringService` for this request."""
    return ScoringService()


ScoringServiceDep = Annotated[ScoringService, Depends(get_scoring_service)]


def get_game_service(
    session_factory: SessionFactoryDep,
    prompt_repository: PromptRepositoryDep,
    scoring_service: ScoringServiceDep,
    settings: SettingsDep,
) -> GameService:
    """Build a :class:`GameService` wired for this request.

    The service is safe to construct per request: it holds only the
    injected collaborators and has no per-instance mutable state.
    """
    return GameService(
        session_factory=session_factory,
        prompt_repository=prompt_repository,
        scoring_service=scoring_service,
        settings=settings,
    )


GameServiceDep = Annotated[GameService, Depends(get_game_service)]


def get_leaderboard_service(
    session_factory: SessionFactoryDep,
) -> LeaderboardService:
    """Build a :class:`LeaderboardService` scoped to this request."""
    return LeaderboardService(session_factory)


LeaderboardServiceDep = Annotated[LeaderboardService, Depends(get_leaderboard_service)]


# ---------------------------------------------------------------------------
# require_player (task 8.7)
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str | None:
    """Parse an ``Authorization: Bearer <token>`` header.

    Returns the raw token on success or ``None`` if the header is
    missing / malformed. Whitespace-only tokens are also treated as
    missing so the downstream ``PlayerService.authorize`` call does
    not pay for a DB round-trip that can never match a stored token.
    """
    if authorization is None:
        return None
    stripped = authorization.strip()
    if not stripped:
        return None
    # Case-insensitive "Bearer" prefix per RFC 6750; tolerate any
    # single whitespace separator.
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def require_player(
    player_service: PlayerServiceDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthorizedPlayer:
    """FastAPI dependency that enforces Session_Token authorization.

    Returns an :class:`AuthorizedPlayer` when the caller presents a
    valid, unexpired Session_Token. Raises
    :class:`app.errors.Unauthorized` (mapped to 401 with the shared
    :class:`app.errors.ApiError` envelope) for every other case:

    * no ``Authorization`` header (``missing``)
    * malformed or non-Bearer header (``missing``)
    * unknown token (``unknown``)
    * expired token (``expired``)

    Requirement 7.3 forbids leaking which sub-case fired, so all
    three paths surface an identical client-facing message. The
    service layer's structured ``reason`` is kept out of the HTTP
    response on purpose — operators can correlate it via logs if
    needed.
    """
    token = _extract_bearer(authorization)
    result = player_service.authorize(token)
    if isinstance(result, Unauthorized):
        raise UnauthorizedApiError(
            message="Session token is missing, unknown, or expired.",
            code=ErrorCode.SESSION_EXPIRED,
        )
    return result


AuthorizedPlayerDep = Annotated[AuthorizedPlayer, Depends(require_player)]


# ---------------------------------------------------------------------------
# Rate limiting (task 9.1 / Requirements 14.1, 14.2, 14.3)
# ---------------------------------------------------------------------------


def _source_ip(request: Request) -> str:
    """Return the source IP used as a rate-limit key.

    ``request.client.host`` is the peer address as seen by the ASGI
    server. We do *not* look at ``X-Forwarded-For`` because the v1
    deployment is a single backend behind no trusted proxy; honoring
    a client-controlled header would let a caller trivially bypass
    the limit. A fallback of ``"unknown"`` keeps the limiter working
    when the transport has no peer info (e.g. certain test clients).
    """
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


def _get_limiter(request: Request, attr: str) -> TokenBucketLimiter:
    limiter: TokenBucketLimiter | None = getattr(request.app.state, attr, None)
    if limiter is None:  # pragma: no cover - defensive startup guard
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Rate limiter '{attr}' is not available.",
        )
    return limiter


def enforce_players_rate_limit(request: Request) -> None:
    """Gate ``POST /players`` on the per-IP bucket.

    Raises :class:`app.errors.RateLimited` (429 ``rate_limited``) when
    the caller has exhausted their bucket. Requirement 14.3 mandates
    that no side effects occur on an over-limit request; running the
    check as a FastAPI dependency ensures we raise *before* the
    endpoint body executes and therefore before :meth:`PlayerService.register`
    is ever called.
    """
    limiter = _get_limiter(request, "players_ip_limiter")
    key = f"ip:{_source_ip(request)}"
    if not limiter.try_acquire(key):
        raise RateLimited(
            "Too many registration attempts. Please try again in a moment.",
            details={"scope": "ip", "endpoint": "POST /players"},
            code=ErrorCode.RATE_LIMITED,
        )


def enforce_games_rate_limit(
    request: Request,
    player: AuthorizedPlayerDep,
) -> AuthorizedPlayer:
    """Gate ``POST /games`` on the per-IP and per-player buckets.

    Requirement 14.2 requires both scopes. We acquire the IP bucket
    first; on success we then acquire the per-player bucket. If the
    player-scoped acquisition fails we do **not** refund the IP-scoped
    token — over-limit calls must have no side effects beyond the
    rejection itself (Requirement 14.3), but we are rejecting either
    way, and "refund on nested failure" would make the IP limiter
    effectively ungated by a malicious client who spams a single
    saturated playerId. Passing one of the two limits still counts
    as a real request that touched the API edge.

    Returns the authorized player so the endpoint can keep its
    existing single-parameter shape by depending on this function
    instead of :func:`require_player`.
    """
    ip_limiter = _get_limiter(request, "games_ip_limiter")
    if not ip_limiter.try_acquire(f"ip:{_source_ip(request)}"):
        raise RateLimited(
            "Too many games started from this client. Please slow down.",
            details={"scope": "ip", "endpoint": "POST /games"},
            code=ErrorCode.RATE_LIMITED,
        )
    player_limiter = _get_limiter(request, "games_player_limiter")
    if not player_limiter.try_acquire(f"player:{player.player_id}"):
        raise RateLimited(
            "Too many games started for this player. Please slow down.",
            details={"scope": "player", "endpoint": "POST /games"},
            code=ErrorCode.RATE_LIMITED,
        )
    return player


GamesRateLimitedPlayerDep = Annotated[
    AuthorizedPlayer, Depends(enforce_games_rate_limit)
]


__all__ = [
    "AuthorizedPlayerDep",
    "EngineDep",
    "GameServiceDep",
    "GamesRateLimitedPlayerDep",
    "LeaderboardServiceDep",
    "PlayerServiceDep",
    "PromptRepositoryDep",
    "ScoringServiceDep",
    "SessionFactoryDep",
    "SettingsDep",
    "enforce_games_rate_limit",
    "enforce_players_rate_limit",
    "get_engine",
    "get_game_service",
    "get_leaderboard_service",
    "get_player_service",
    "get_prompt_repository",
    "get_scoring_service",
    "get_session_factory",
    "require_player",
    "settings_dependency",
]
