"""Games REST endpoints (tasks 8.2, 8.3, 8.4, 8.6).

Four routes live on this router:

- ``POST /games`` — start a new Game (auth required, task 8.2).
- ``POST /games/{gameId}/begin`` — mark typing started (auth, task 8.3).
- ``POST /games/{gameId}/result`` — submit the final text (auth, task 8.4).
- ``GET /games/{gameId}`` — fetch metadata (no auth, task 8.6).

The first three are gated by :func:`require_player`. ``GET /games``
is public so a reconnecting client can resume without having the
token to hand yet (though the design reserves the right to lock it
later — the service result types already carry everything needed
for a future ``player_id`` check).

Requirements addressed:
- 2.2, 2.3, 2.4, 2.6, 7.2 (POST /games)
- 3.2, 6.4, 8.2, 15.1 (POST /games/{id}/begin)
- 3.5, 3.6, 4.6, 4.7, 9.2 (POST /games/{id}/result)
- 12.1, 12.2 (GET /games/{id})
"""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ..errors import (
    Conflict,
    ErrorCode,
    GameTimeout,
    NotFound,
    ValidationFailed,
)
from ..persistence.models import Game, Prompt
from ..services import (
    BeginTypingSuccess,
    CompleteGameSuccess,
    CompleteGameTimeout,
    CreateGameSuccess,
    GameAlreadyInProgress,
    GameNotFound,
    GameNotInPending,
    GameNotInProgress,
    PlayerNotFound,
)
from .dependencies import (
    AuthorizedPlayerDep,
    GameServiceDep,
    GamesRateLimitedPlayerDep,
    LeaderboardServiceDep,
    SessionFactoryDep,
)
from .schemas import (
    BeginGameResponse,
    CreateGameResponse,
    GameMetadataResponse,
    SubmitResultRequest,
    SubmitResultResponse,
    TYPED_TEXT_SLACK_CHARS,
)


router = APIRouter(tags=["games"])


# ---------------------------------------------------------------------------
# POST /games (task 8.2)
# ---------------------------------------------------------------------------


@router.post(
    "/games",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateGameResponse,
    response_model_by_alias=True,
)
def create_game(
    player: GamesRateLimitedPlayerDep,
    game_service: GameServiceDep,
) -> CreateGameResponse:
    """Create a new Game for the authenticated player.

    Auth is performed by the ``enforce_games_rate_limit`` dependency
    (which itself depends on :func:`require_player`), so the endpoint
    sees an :class:`AuthorizedPlayer` only when both the session
    token is valid *and* the per-IP and per-player rate limits are
    below their caps. Over-limit requests therefore raise
    :class:`app.errors.RateLimited` before this body runs, which
    means no Game is created on a 429 — Requirement 14.3 / Property
    19.

    Requirements 2.3 / 2.4: the response includes ``gameId``, the
    prompt text, and a server-determined ``startAt`` the client uses
    to align its countdown. On conflict (Requirement 2.6) the 409
    body carries ``existingGameId`` so the Web_Client can route the
    player into the existing Game.
    """
    result = game_service.create_game(player.player_id)

    if isinstance(result, PlayerNotFound):  # pragma: no cover - auth-guarded
        # Should be unreachable because ``require_player`` already
        # verified the player exists. Surface 401 rather than 500 so
        # any race between registration and deletion doesn't leak.
        raise Conflict(
            "Player not found.",
            details={"playerId": result.player_id},
            code=ErrorCode.SESSION_EXPIRED,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if isinstance(result, GameAlreadyInProgress):
        raise Conflict(
            "A game is already in progress for this player.",
            details={
                "existingGameId": result.game_id,
                "status": result.status.value,
            },
            code=ErrorCode.GAME_CONFLICT,
        )

    assert isinstance(result, CreateGameSuccess)
    return CreateGameResponse(
        game_id=result.game_id,
        prompt_id=result.prompt_id,
        prompt=result.prompt_text,
        language=result.language,
        status=result.status.value,
        start_at=result.started_at,
    )


# ---------------------------------------------------------------------------
# POST /games/{gameId}/begin (task 8.3)
# ---------------------------------------------------------------------------


@router.post(
    "/games/{game_id}/begin",
    status_code=status.HTTP_200_OK,
    response_model=BeginGameResponse,
    response_model_by_alias=True,
)
def begin_game(
    game_id: str,
    player: AuthorizedPlayerDep,
    game_service: GameServiceDep,
) -> BeginGameResponse:
    """Transition the Game ``pending → in_progress``.

    Records the authoritative server ``startedAt`` (Requirement 15.1)
    used later by the Scoring_Service for elapsed-time calculation.
    """
    result = game_service.begin_typing(game_id, player_id=player.player_id)

    if isinstance(result, GameNotFound):
        raise NotFound(
            "Game not found.",
            details={"gameId": result.game_id},
        )

    if isinstance(result, GameNotInPending):
        raise Conflict(
            "Game cannot be begun from its current status.",
            details={
                "gameId": result.game_id,
                "currentStatus": result.current_status.value,
            },
            code=ErrorCode.GAME_CONFLICT,
        )

    assert isinstance(result, BeginTypingSuccess)
    return BeginGameResponse(
        game_id=result.game_id,
        status=result.status.value,
        started_at=result.started_at,
        prompt_id=result.prompt_id,
        prompt=result.prompt_text,
    )


# ---------------------------------------------------------------------------
# POST /games/{gameId}/result (task 8.4)
# ---------------------------------------------------------------------------


@router.post(
    "/games/{game_id}/result",
    status_code=status.HTTP_200_OK,
    response_model=SubmitResultResponse,
    response_model_by_alias=True,
)
def submit_result(
    game_id: str,
    body: SubmitResultRequest,
    player: AuthorizedPlayerDep,
    game_service: GameServiceDep,
    leaderboard_service: LeaderboardServiceDep,
    session_factory: SessionFactoryDep,
) -> SubmitResultResponse:
    """Finalize a typing attempt and return the player's Score + rank.

    The body's ``elapsedSeconds`` is intentionally unused; the server
    computes elapsed from ``endedAt - startedAt`` per Requirements
    3.6 / 15.1 / 15.2. On timeout (Requirement 9.2 / Property 14) we
    return 409 ``game_timeout``.

    Task 9.4 / Requirement 13.2: the endpoint enforces a dynamic
    per-Game upper bound on ``typedText`` of
    ``len(prompt.text) + TYPED_TEXT_SLACK_CHARS`` before handing the
    text to the Scoring_Service. This rejects abusive submissions
    (e.g. megabyte-sized payloads that somehow slipped past the
    Pydantic layer or pathological overshoots) with 400
    ``validation_error`` before any Game transition or Score write.
    The check runs against the authenticated player's own Game so
    ownership mismatches still surface via
    :meth:`GameService.complete` as 404 (existence is not leaked).
    """
    # Task 9.4: look up the Game's prompt length so we can enforce a
    # dynamic typed-text bound. If the Game is unknown we fall
    # through to ``game_service.complete`` which returns GameNotFound
    # → 404, matching the behavior for every other unknown-game
    # path. If the Game exists but belongs to another player we
    # also fall through: the service enforces ownership via its
    # step-2 check and returns GameNotFound for that case too
    # (documented in :meth:`GameService.complete`), which collapses
    # to 404 to avoid leaking existence.
    with session_factory() as session:
        stmt = select(Prompt.text).join(Game, Game.prompt_id == Prompt.id).where(
            Game.id == game_id,
            Game.player_id == player.player_id,
        )
        prompt_text = session.execute(stmt).scalar_one_or_none()

    if prompt_text is not None:
        max_typed = len(prompt_text) + TYPED_TEXT_SLACK_CHARS
        if len(body.typed_text) > max_typed:
            raise ValidationFailed(
                "Typed text exceeds the allowed length for this prompt.",
                details={
                    "field": "typedText",
                    "reason": "too_long",
                    "promptLength": len(prompt_text),
                    "slack": TYPED_TEXT_SLACK_CHARS,
                    "maxAllowed": max_typed,
                    "actual": len(body.typed_text),
                },
                code=ErrorCode.VALIDATION_ERROR,
            )

    result = game_service.complete(
        game_id,
        body.typed_text,
        player_id=player.player_id,
    )

    if isinstance(result, GameNotFound):
        raise NotFound(
            "Game not found.",
            details={"gameId": result.game_id},
        )

    if isinstance(result, CompleteGameTimeout):
        raise GameTimeout(
            "Time's up — the game exceeded the maximum duration.",
            details={
                "gameId": result.game_id,
                "endedAt": result.ended_at.isoformat(),
                "elapsedSeconds": result.elapsed_seconds,
            },
        )

    if isinstance(result, GameNotInProgress):
        raise Conflict(
            "Game cannot be completed from its current status.",
            details={
                "gameId": result.game_id,
                "currentStatus": result.current_status.value,
            },
            code=ErrorCode.GAME_CONFLICT,
        )

    assert isinstance(result, CompleteGameSuccess)

    # Look up the player's current rank on the leaderboard. The
    # LeaderboardService returns entries already ordered and
    # rank-assigned, so we just find this player's row. If somehow
    # the leaderboard has no entry yet (e.g. a concurrent reader
    # observed an intermediate state) we report rank 1 — the Score
    # was just persisted, so it must be at least the top entry for
    # this player.
    snapshot = leaderboard_service.build_snapshot()
    rank = 0
    for entry in snapshot.entries:
        if entry.player_id == result.player_id:
            rank = entry.rank
            break
    if rank == 0:  # pragma: no cover - defensive
        rank = 1

    return SubmitResultResponse(
        game_id=result.game_id,
        wpm=result.wpm,
        accuracy=result.accuracy,
        points=result.points,
        rank=rank,
        ended_at=result.ended_at,
    )


# ---------------------------------------------------------------------------
# GET /games/{gameId} (task 8.6)
# ---------------------------------------------------------------------------


@router.get(
    "/games/{game_id}",
    status_code=status.HTTP_200_OK,
    response_model=GameMetadataResponse,
    response_model_by_alias=True,
)
def get_game(
    game_id: str,
    session_factory: SessionFactoryDep,
) -> GameMetadataResponse:
    """Return metadata for a Game.

    Requirement 12.1 asks for the Game's prompt text and current
    status. We load both in a single query (joining the Prompt row)
    so there is no second round-trip for the prompt text. An unknown
    ``gameId`` returns 404 (Requirement 12.2).

    The endpoint is unauthenticated: the design's contract for
    ``/games/{gameId}`` is that any client with the id can fetch
    metadata to reconnect or show post-game details. If that
    changes, swap in :func:`require_player` and compare ``player_id``.
    """
    with session_factory() as session:
        stmt = (
            select(Game)
            .options(joinedload(Game.player), joinedload(Game.score))
            .where(Game.id == game_id)
        )
        # Fetch the joined prompt separately to avoid relationship
        # pollution — the ORM model doesn't define a relationship
        # from Game to Prompt, so we explicitly load by id.
        row = session.execute(stmt).unique().scalar_one_or_none()
        if row is None:
            raise NotFound(
                "Game not found.",
                details={"gameId": game_id},
            )

        prompt = session.get(Prompt, row.prompt_id)
        if prompt is None:  # pragma: no cover - FK invariant
            raise NotFound(
                "Prompt for game is missing.",
                details={"gameId": game_id},
            )

        return GameMetadataResponse(
            game_id=row.id,
            player_id=row.player_id,
            prompt_id=prompt.id,
            prompt=prompt.text,
            language=prompt.language,
            status=row.status.value,
            started_at=row.started_at,
            ended_at=row.ended_at,
        )


__all__ = ["router"]
