"""Deterministic unit tests for ``GameService.begin_typing`` (task 4.3).

Covers the five cases called out by the task brief:

1. Happy path: ``pending → in_progress``, ``started_at`` recorded from
   the injected clock, response payload carries the prompt text.
2. Game not found: unknown ``game_id`` returns :class:`GameNotFound`
   without mutating any row.
3. Ownership: a Game that exists under a different player returns
   :class:`GameNotFound` (the endpoint must not leak existence across
   players).
4. Already in progress: returns :class:`GameNotInPending` with
   ``current_status = IN_PROGRESS`` and does not overwrite the
   persisted ``started_at``.
5. Terminal statuses: ``completed`` and ``abandoned`` return
   :class:`GameNotInPending` with the observed status and leave the
   row untouched.

The ``clock`` and ``id_factory`` are pinned so the assertions are
fully deterministic; property-based coverage of the state machine
lives in task 4.6's test file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    PromptRepository,
    init_db,
)
from app.services import (
    BeginTypingSuccess,
    GameNotFound,
    GameNotInPending,
    GameService,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(eng)
    return eng


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _make_player(session_factory: sessionmaker[Session], *, nickname: str) -> Player:
    """Insert a Player row and return a detached snapshot."""
    with session_factory() as s:
        player = Player(
            id=str(uuid.uuid4()),
            nickname=nickname,
            nickname_ci=nickname.lower(),
            session_token=str(uuid.uuid4()),
            session_expires_at=_future(),
        )
        s.add(player)
        s.commit()
        return Player(
            id=player.id,
            nickname=player.nickname,
            nickname_ci=player.nickname_ci,
            session_token=player.session_token,
            session_expires_at=player.session_expires_at,
        )


def _make_prompt(session_factory: sessionmaker[Session]) -> Prompt:
    """Insert a single prompt row with a text body valid under seed rules."""
    with session_factory() as s:
        prompt = Prompt(
            id=str(uuid.uuid4()),
            text="the quick brown fox jumps over the lazy dog. " * 4,
            difficulty=None,
            language="en",
        )
        s.add(prompt)
        s.commit()
        return Prompt(
            id=prompt.id,
            text=prompt.text,
            difficulty=prompt.difficulty,
            language=prompt.language,
        )


def _make_game_row(
    session_factory: sessionmaker[Session],
    *,
    player: Player,
    prompt: Prompt,
    status: GameStatus,
) -> str:
    """Insert a Game row with the given status and return its id.

    Timestamps are supplied only for statuses that need them, so the
    ``ended_at > started_at`` CHECK constraint stays satisfied.
    """
    now = datetime.now(timezone.utc)
    started_at: datetime | None
    ended_at: datetime | None
    if status is GameStatus.PENDING:
        started_at, ended_at = None, None
    elif status is GameStatus.IN_PROGRESS:
        started_at, ended_at = now - timedelta(seconds=5), None
    elif status is GameStatus.COMPLETED:
        started_at = now - timedelta(seconds=30)
        ended_at = now - timedelta(seconds=1)
    else:  # ABANDONED
        started_at = now - timedelta(seconds=60)
        ended_at = now - timedelta(seconds=5)

    with session_factory() as s:
        row = Game(
            id=str(uuid.uuid4()),
            player_id=player.id,
            prompt_id=prompt.id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
        )
        s.add(row)
        s.commit()
        return row.id


def _fixed_clock(initial: datetime) -> Callable[[], datetime]:
    state = {"now": initial}

    def clock() -> datetime:
        return state["now"]

    return clock


def _service(
    session_factory: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] | None = None,
) -> GameService:
    """Wire a ``GameService`` with the shared in-process repository.

    ``begin_typing`` does not call the Prompt_Repository, but the
    service constructor still requires one, so we hand it a repo
    wired against the same session factory (harmless).
    """
    repo = PromptRepository(session_factory)
    if clock is None:
        return GameService(session_factory, repo)
    return GameService(session_factory, repo, clock=clock)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_begin_typing_transitions_pending_game_to_in_progress(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Alice")
    prompt = _make_prompt(session_factory)
    game_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.PENDING,
    )

    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    service = _service(session_factory, clock=_fixed_clock(t0))

    result = service.begin_typing(game_id, player_id=player.id)

    # Return payload.
    assert isinstance(result, BeginTypingSuccess)
    assert result.game_id == game_id
    assert result.status is GameStatus.IN_PROGRESS
    assert result.started_at == t0
    assert result.prompt_id == prompt.id
    assert result.prompt_text == prompt.text

    # Row mutated exactly as described. SQLite's ``DateTime(timezone=True)``
    # round-trips to a naive datetime on read, so compare naive forms.
    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is GameStatus.IN_PROGRESS
        assert row.started_at is not None
        assert row.started_at.replace(tzinfo=timezone.utc) == t0
        assert row.ended_at is None


# ---------------------------------------------------------------------------
# 2. Game not found
# ---------------------------------------------------------------------------


def test_begin_typing_returns_not_found_for_unknown_game_id(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Bob")
    service = _service(session_factory)

    unknown = str(uuid.uuid4())
    result = service.begin_typing(unknown, player_id=player.id)

    assert isinstance(result, GameNotFound)
    assert result.game_id == unknown

    # No rows anywhere got inserted.
    with session_factory() as s:
        assert s.execute(select(Game)).scalars().all() == []


# ---------------------------------------------------------------------------
# 3. Ownership: different player's game is reported as not-found
# ---------------------------------------------------------------------------


def test_begin_typing_returns_not_found_when_game_belongs_to_other_player(
    session_factory: sessionmaker[Session],
) -> None:
    """The endpoint must not leak existence across players.

    Requirement 7.2 scopes protected endpoints to the session's
    ``player_id``. Folding the ownership mismatch into
    :class:`GameNotFound` keeps the endpoint from advertising the
    game-id namespace of other players — a caller who guesses a
    ``gameId`` should get the same response whether it belongs to
    someone else or does not exist at all.
    """
    owner = _make_player(session_factory, nickname="Carol")
    intruder = _make_player(session_factory, nickname="Dave")
    prompt = _make_prompt(session_factory)
    game_id = _make_game_row(
        session_factory,
        player=owner,
        prompt=prompt,
        status=GameStatus.PENDING,
    )

    service = _service(session_factory)
    result = service.begin_typing(game_id, player_id=intruder.id)

    assert isinstance(result, GameNotFound)
    assert result.game_id == game_id

    # Row is untouched (still pending, no started_at).
    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is GameStatus.PENDING
        assert row.started_at is None
        assert row.ended_at is None


# ---------------------------------------------------------------------------
# 4. Already in progress
# ---------------------------------------------------------------------------


def test_begin_typing_returns_not_in_pending_when_already_in_progress(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Eve")
    prompt = _make_prompt(session_factory)
    game_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
    )

    # Snapshot the originally-persisted started_at so we can assert it
    # is preserved (the second begin_typing call must not overwrite it,
    # per Requirement 8.5 — "leave status unchanged").
    with session_factory() as s:
        original_row = s.get(Game, game_id)
        assert original_row is not None
        original_started_at = original_row.started_at

    # Pin the service clock far in the future — if the service wrote
    # anything it would be obvious in the persisted row.
    t_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    service = _service(session_factory, clock=_fixed_clock(t_future))

    result = service.begin_typing(game_id, player_id=player.id)

    assert isinstance(result, GameNotInPending)
    assert result.game_id == game_id
    assert result.current_status is GameStatus.IN_PROGRESS

    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is GameStatus.IN_PROGRESS
        assert row.started_at == original_started_at
        assert row.ended_at is None


# ---------------------------------------------------------------------------
# 5. Terminal statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_status",
    [GameStatus.COMPLETED, GameStatus.ABANDONED],
)
def test_begin_typing_returns_not_in_pending_for_terminal_statuses(
    session_factory: sessionmaker[Session],
    terminal_status: GameStatus,
) -> None:
    player = _make_player(session_factory, nickname=f"F-{terminal_status.value}")
    prompt = _make_prompt(session_factory)
    game_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=terminal_status,
    )

    with session_factory() as s:
        original_row = s.get(Game, game_id)
        assert original_row is not None
        original_started_at = original_row.started_at
        original_ended_at = original_row.ended_at

    t_future = datetime(2099, 6, 1, tzinfo=timezone.utc)
    service = _service(session_factory, clock=_fixed_clock(t_future))

    result = service.begin_typing(game_id, player_id=player.id)

    assert isinstance(result, GameNotInPending)
    assert result.game_id == game_id
    assert result.current_status is terminal_status

    # Row untouched.
    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is terminal_status
        assert row.started_at == original_started_at
        assert row.ended_at == original_ended_at
