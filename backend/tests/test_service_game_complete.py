"""Deterministic unit tests for ``GameService.complete`` (task 4.4).

Covers:

1. Happy path: ``in_progress → completed`` with a Score row persisted.
2. Game not found: unknown id returns :class:`GameNotFound`; no row
   touched.
3. Ownership: a Game that belongs to another player returns
   :class:`GameNotFound` (existence must not leak across players).
4. Wrong status: ``pending`` / ``completed`` / ``abandoned`` return
   :class:`GameNotInProgress` with the observed status; nothing is
   written.
5. Timeout: elapsed > ``Settings.max_game_duration_seconds`` returns
   :class:`CompleteGameTimeout`; the Game row transitions to
   ``abandoned`` with ``ended_at`` set; no Score row is written.
6. Idempotency: calling complete twice for the same game returns
   :class:`GameNotInProgress` on the second call.
7. Server-authoritative timing: scoring ignores typed-text-derived
   elapsed; only ``(ended_at - started_at)`` matters.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.scoring import compute_accuracy, compute_points, compute_wpm
from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    PromptRepository,
    Score,
    init_db,
)
from app.services import (
    CompleteGameSuccess,
    CompleteGameTimeout,
    GameNotFound,
    GameNotInProgress,
    GameService,
    ScoringService,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
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
# Helpers
# ---------------------------------------------------------------------------


PROMPT_TEXT = "the quick brown fox jumps over the lazy dog"
MAX_DURATION = 120  # seconds; matches Settings default but pinned for clarity


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _make_player(session_factory: sessionmaker[Session], *, nickname: str) -> Player:
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
    with session_factory() as s:
        prompt = Prompt(
            id=str(uuid.uuid4()),
            text=PROMPT_TEXT,
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


def _make_game(
    session_factory: sessionmaker[Session],
    *,
    player: Player,
    prompt: Prompt,
    status: GameStatus,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> str:
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
    clock: Callable[[], datetime],
    max_duration_seconds: int = MAX_DURATION,
) -> GameService:
    settings = Settings(max_game_duration_seconds=max_duration_seconds)
    scoring = ScoringService()
    repo = PromptRepository(session_factory)
    return GameService(
        session_factory,
        repo,
        clock=clock,
        scoring_service=scoring,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_complete_happy_path_persists_score_and_transitions_game(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Alice")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=30)
    game_id = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = _service(session_factory, clock=_fixed_clock(ended_at))

    result = service.complete(game_id, PROMPT_TEXT, player_id=player.id)

    assert isinstance(result, CompleteGameSuccess)
    assert result.game_id == game_id
    assert result.player_id == player.id
    assert result.ended_at == ended_at
    assert result.elapsed_seconds == 30.0

    expected_wpm = compute_wpm(PROMPT_TEXT, PROMPT_TEXT, 30.0)
    expected_acc = compute_accuracy(PROMPT_TEXT, PROMPT_TEXT)
    expected_pts = compute_points(expected_wpm, expected_acc)
    assert result.wpm == expected_wpm
    assert result.accuracy == expected_acc
    assert result.points == expected_pts

    # DB: Score persisted, Game transitioned.
    with session_factory() as s:
        score = s.execute(select(Score).where(Score.game_id == game_id)).scalar_one()
        assert score.id == result.score_id
        assert score.wpm == expected_wpm
        assert score.accuracy == expected_acc
        assert score.points == expected_pts

        game = s.get(Game, game_id)
        assert game is not None
        assert game.status is GameStatus.COMPLETED
        observed = game.ended_at
        assert observed is not None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        assert observed == ended_at


# ---------------------------------------------------------------------------
# 2. Game not found
# ---------------------------------------------------------------------------


def test_complete_returns_not_found_for_unknown_game(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Bob")
    service = _service(
        session_factory,
        clock=_fixed_clock(datetime.now(timezone.utc)),
    )

    unknown = str(uuid.uuid4())
    result = service.complete(unknown, "whatever", player_id=player.id)

    assert isinstance(result, GameNotFound)
    assert result.game_id == unknown

    with session_factory() as s:
        assert s.execute(select(Score)).scalars().all() == []


# ---------------------------------------------------------------------------
# 3. Ownership: different player → not found
# ---------------------------------------------------------------------------


def test_complete_returns_not_found_when_game_belongs_to_other_player(
    session_factory: sessionmaker[Session],
) -> None:
    owner = _make_player(session_factory, nickname="Carol")
    intruder = _make_player(session_factory, nickname="Dave")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    game_id = _make_game(
        session_factory,
        player=owner,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = _service(
        session_factory,
        clock=_fixed_clock(started_at + timedelta(seconds=10)),
    )
    result = service.complete(game_id, PROMPT_TEXT, player_id=intruder.id)

    assert isinstance(result, GameNotFound)
    assert result.game_id == game_id

    with session_factory() as s:
        assert s.execute(select(Score)).scalars().all() == []
        game = s.get(Game, game_id)
        assert game is not None
        assert game.status is GameStatus.IN_PROGRESS
        assert game.ended_at is None


# ---------------------------------------------------------------------------
# 4. Wrong status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [GameStatus.PENDING, GameStatus.COMPLETED, GameStatus.ABANDONED],
)
def test_complete_rejects_game_not_in_progress(
    session_factory: sessionmaker[Session], status: GameStatus
) -> None:
    player = _make_player(session_factory, nickname=f"E-{status.value}")
    prompt = _make_prompt(session_factory)

    if status is GameStatus.PENDING:
        started_at, ended_at_seed = None, None
    elif status is GameStatus.COMPLETED:
        started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ended_at_seed = started_at + timedelta(seconds=10)
    else:
        started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ended_at_seed = started_at + timedelta(seconds=5)

    game_id = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=status,
        started_at=started_at,
        ended_at=ended_at_seed,
    )

    service = _service(
        session_factory,
        clock=_fixed_clock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )
    result = service.complete(game_id, PROMPT_TEXT, player_id=player.id)

    assert isinstance(result, GameNotInProgress)
    assert result.game_id == game_id
    assert result.current_status is status

    # No Score written.
    with session_factory() as s:
        assert s.execute(
            select(Score).where(Score.game_id == game_id)
        ).scalars().all() == []


# ---------------------------------------------------------------------------
# 5. Timeout
# ---------------------------------------------------------------------------


def test_complete_timeout_transitions_to_abandoned_and_writes_no_score(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Frank")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Exceed the max duration explicitly.
    ended_at = started_at + timedelta(seconds=MAX_DURATION + 1)
    game_id = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = _service(
        session_factory,
        clock=_fixed_clock(ended_at),
        max_duration_seconds=MAX_DURATION,
    )

    result = service.complete(game_id, PROMPT_TEXT, player_id=player.id)

    assert isinstance(result, CompleteGameTimeout)
    assert result.game_id == game_id
    assert result.ended_at == ended_at
    assert result.elapsed_seconds == float(MAX_DURATION + 1)

    # Game transitioned to ABANDONED, Score not written.
    with session_factory() as s:
        game = s.get(Game, game_id)
        assert game is not None
        assert game.status is GameStatus.ABANDONED
        observed = game.ended_at
        assert observed is not None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        assert observed == ended_at

        assert s.execute(
            select(Score).where(Score.game_id == game_id)
        ).scalars().all() == []


def test_complete_does_not_timeout_at_exact_max_duration(
    session_factory: sessionmaker[Session],
) -> None:
    """At ``elapsed == max_game_duration_seconds`` the submission scores.

    The timeout branch is strictly ``elapsed > max_duration`` so a
    clean finish at the boundary still completes normally. This keeps
    the timeout behaviour aligned with Property 14's "exceeds" framing.
    """
    player = _make_player(session_factory, nickname="Grace")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=MAX_DURATION)
    game_id = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = _service(
        session_factory,
        clock=_fixed_clock(ended_at),
        max_duration_seconds=MAX_DURATION,
    )
    result = service.complete(game_id, PROMPT_TEXT, player_id=player.id)

    assert isinstance(result, CompleteGameSuccess)
    assert result.elapsed_seconds == float(MAX_DURATION)

    # Score written.
    with session_factory() as s:
        score = s.execute(
            select(Score).where(Score.game_id == game_id)
        ).scalar_one_or_none()
        assert score is not None


# ---------------------------------------------------------------------------
# 6. Idempotency: second call returns GameNotInProgress
# ---------------------------------------------------------------------------


def test_complete_second_call_returns_not_in_progress(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Heidi")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=30)
    game_id = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    # First call completes; second call is rejected.
    clock_state = {"now": ended_at}

    def clock() -> datetime:
        return clock_state["now"]

    service = _service(session_factory, clock=clock)

    first = service.complete(game_id, PROMPT_TEXT, player_id=player.id)
    assert isinstance(first, CompleteGameSuccess)

    # Advance the clock to simulate a retry. Game is now COMPLETED.
    clock_state["now"] = ended_at + timedelta(seconds=5)

    second = service.complete(game_id, PROMPT_TEXT, player_id=player.id)
    assert isinstance(second, GameNotInProgress)
    assert second.current_status is GameStatus.COMPLETED

    # Still exactly one Score.
    with session_factory() as s:
        rows = s.execute(
            select(Score).where(Score.game_id == game_id)
        ).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 7. Server-authoritative timing: longer typed text doesn't change scoring
# ---------------------------------------------------------------------------


def test_complete_scoring_depends_on_server_elapsed_only(
    session_factory: sessionmaker[Session],
) -> None:
    """Two games with identical server elapsed must produce identical WPM.

    This is the service-level echo of Property 7 from the design
    document. We can't perturb the "client-supplied elapsed" here
    because the API doesn't accept one on this code path — the whole
    point is that the service never reads it. Instead we fix the
    server elapsed and assert the score is a pure function of it.
    """
    player = _make_player(session_factory, nickname="Ivan")
    prompt = _make_prompt(session_factory)

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=30)

    # Two independent games, same timing, same typed text.
    g1 = _make_game(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = _service(session_factory, clock=_fixed_clock(ended_at))
    r1 = service.complete(g1, PROMPT_TEXT, player_id=player.id)
    assert isinstance(r1, CompleteGameSuccess)

    # Second player / game with identical server elapsed.
    player2 = _make_player(session_factory, nickname="Judy")
    g2 = _make_game(
        session_factory,
        player=player2,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )
    service2 = _service(session_factory, clock=_fixed_clock(ended_at))
    r2 = service2.complete(g2, PROMPT_TEXT, player_id=player2.id)
    assert isinstance(r2, CompleteGameSuccess)

    assert r1.wpm == r2.wpm
    assert r1.accuracy == r2.accuracy
    assert r1.points == r2.points
