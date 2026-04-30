"""Unit tests for the timeout sweeper (task 4.5).

Covers two surfaces:

1. :meth:`GameService.sweep_timeouts` — the synchronous decision
   logic. Seeds a mix of Games in every status (plus edge rows like
   ``in_progress`` with ``started_at IS NULL`` and ``in_progress``
   within the duration) and asserts that only rows past the
   Maximum_Game_Duration are transitioned to ``abandoned`` with
   ``ended_at`` set, and the returned list contains only those rows.

2. :class:`TimeoutSweeper.run` — the async loop wraps the service
   call and sleeps for ``interval_seconds`` between ticks. Tested
   with a fake game service whose ``sweep_timeouts`` signals an
   :class:`asyncio.Event` each call; the test waits for a couple of
   signals then cancels the task and asserts a clean exit.

Requirements addressed: 9.1, 9.4.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    PromptRepository,
    init_db,
)
from app.services import (
    GameService,
    ScoringService,
    SweptGame,
    TimeoutSweeper,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


MAX_DURATION = 60  # seconds; pinned for deterministic cutoff math.
PROMPT_TEXT = "the quick brown fox jumps over the lazy dog " * 3


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


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _make_player(session_factory: sessionmaker[Session], *, nickname: str) -> str:
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
        return player.id


def _make_prompt(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as s:
        prompt = Prompt(
            id=str(uuid.uuid4()),
            text=PROMPT_TEXT,
            difficulty=None,
            language="en",
        )
        s.add(prompt)
        s.commit()
        return prompt.id


def _make_game(
    session_factory: sessionmaker[Session],
    *,
    player_id: str,
    prompt_id: str,
    status: GameStatus,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> str:
    with session_factory() as s:
        row = Game(
            id=str(uuid.uuid4()),
            player_id=player_id,
            prompt_id=prompt_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
        )
        s.add(row)
        s.commit()
        return row.id


def _service(
    session_factory: sessionmaker[Session],
    *,
    max_duration_seconds: int = MAX_DURATION,
) -> GameService:
    settings = Settings(max_game_duration_seconds=max_duration_seconds)
    scoring = ScoringService()
    repo = PromptRepository(session_factory)
    # The clock passed here is only used as a fallback; every test
    # below drives ``sweep_timeouts`` with an explicit ``now`` so the
    # cutoff math is fully deterministic.
    fixed_clock = lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    return GameService(
        session_factory,
        repo,
        clock=fixed_clock,
        scoring_service=scoring,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# 1. GameService.sweep_timeouts — mixed population
# ---------------------------------------------------------------------------


def test_sweep_timeouts_abandons_only_timed_out_in_progress_games(
    session_factory: sessionmaker[Session],
) -> None:
    """Requirements 9.1 / 9.4: only timed-out ``in_progress`` rows flip."""
    player_id = _make_player(session_factory, nickname="Alice")
    prompt_id = _make_prompt(session_factory)

    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # One second past the cutoff — definitely timed out.
    past_start = now - timedelta(seconds=MAX_DURATION + 1)
    # One second before the cutoff — still within the duration.
    recent_start = now - timedelta(seconds=MAX_DURATION - 1)

    # Seed one of each interesting row:
    pending_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.PENDING,
    )
    recent_in_progress_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.IN_PROGRESS,
        started_at=recent_start,
    )
    timed_out_a_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.IN_PROGRESS,
        started_at=past_start,
    )
    timed_out_b_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.IN_PROGRESS,
        started_at=past_start - timedelta(seconds=30),
    )
    completed_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.COMPLETED,
        started_at=past_start,
        ended_at=past_start + timedelta(seconds=10),
    )
    abandoned_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.ABANDONED,
        started_at=past_start,
        ended_at=past_start + timedelta(seconds=20),
    )

    service = _service(session_factory)

    swept = service.sweep_timeouts(now=now)

    # --- Return value: only the two timed-out rows are swept.
    assert isinstance(swept, list)
    assert all(isinstance(s, SweptGame) for s in swept)
    swept_ids = {s.game_id for s in swept}
    assert swept_ids == {timed_out_a_id, timed_out_b_id}

    # Each entry carries the expected fields.
    for entry in swept:
        assert entry.player_id == player_id
        assert entry.ended_at == now
        assert entry.started_at is not None
        # ``started_at`` must predate ``ended_at`` — consistent with
        # the ``ended_at > started_at`` invariant the DB also enforces.
        assert entry.ended_at > entry.started_at

    # --- DB state: timed-out rows are now abandoned with ended_at=now.
    with session_factory() as s:
        for gid in (timed_out_a_id, timed_out_b_id):
            row = s.get(Game, gid)
            assert row is not None
            assert row.status is GameStatus.ABANDONED
            ended = row.ended_at
            assert ended is not None
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            assert ended == now

        # Untouched rows keep their original status and ended_at.
        pending_row = s.get(Game, pending_id)
        assert pending_row is not None
        assert pending_row.status is GameStatus.PENDING
        assert pending_row.ended_at is None

        recent_row = s.get(Game, recent_in_progress_id)
        assert recent_row is not None
        assert recent_row.status is GameStatus.IN_PROGRESS
        assert recent_row.ended_at is None

        completed_row = s.get(Game, completed_id)
        assert completed_row is not None
        assert completed_row.status is GameStatus.COMPLETED

        abandoned_row = s.get(Game, abandoned_id)
        assert abandoned_row is not None
        assert abandoned_row.status is GameStatus.ABANDONED


def test_sweep_timeouts_returns_empty_when_nothing_due(
    session_factory: sessionmaker[Session],
) -> None:
    """No rows due → empty list, no writes."""
    player_id = _make_player(session_factory, nickname="Bob")
    prompt_id = _make_prompt(session_factory)

    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    recent_start = now - timedelta(seconds=MAX_DURATION - 5)

    _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.IN_PROGRESS,
        started_at=recent_start,
    )
    _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.PENDING,
    )

    service = _service(session_factory)

    swept = service.sweep_timeouts(now=now)

    assert swept == []


def test_sweep_timeouts_ignores_in_progress_with_null_started_at(
    session_factory: sessionmaker[Session],
) -> None:
    """A corrupted ``in_progress`` row with NULL ``started_at`` must not be swept."""
    player_id = _make_player(session_factory, nickname="Carol")
    prompt_id = _make_prompt(session_factory)

    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    corrupted_id = _make_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        status=GameStatus.IN_PROGRESS,
        started_at=None,
    )

    service = _service(session_factory)

    swept = service.sweep_timeouts(now=now)

    assert swept == []
    with session_factory() as s:
        row = s.get(Game, corrupted_id)
        assert row is not None
        assert row.status is GameStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 2. TimeoutSweeper — async loop
# ---------------------------------------------------------------------------


class _FakeGameService:
    """Minimal stand-in for :class:`GameService` in the async loop test.

    Records every ``sweep_timeouts`` call and signals an
    :class:`asyncio.Event` so the test can await tick completion
    without sleeping-and-hoping.
    """

    def __init__(self) -> None:
        self.calls: list[datetime | None] = []
        self.tick = asyncio.Event()

    def sweep_timeouts(
        self, now: datetime | None = None
    ) -> list[SweptGame]:  # matches GameService signature
        self.calls.append(now)
        # ``set`` + immediate ``clear`` would race with the waiter;
        # set and let the waiter clear before the next await.
        self.tick.set()
        return []


@pytest.mark.asyncio
async def test_timeout_sweeper_run_calls_service_until_cancelled() -> None:
    """``run`` keeps invoking ``sweep_timeouts`` until cancelled, then exits cleanly."""
    fake = _FakeGameService()
    sweeper = TimeoutSweeper(
        fake,  # type: ignore[arg-type]
        interval_seconds=1,  # reassigned below via a tight sleep surrogate
    )
    # Swap in a tiny interval that the constructor's positive-int
    # guard rejects at construction time. Rather than relax that
    # guard, patch the private attribute here — the class contract
    # from the app's point of view is "positive integer seconds";
    # tests are free to drive it faster.
    sweeper._interval_seconds = 0.01  # type: ignore[assignment]

    task = await sweeper.start()

    try:
        # Wait for at least two ticks to observe the loop actually
        # looping rather than completing once. Each tick sets the
        # event; we clear it between awaits so the next ``wait``
        # blocks until the next tick.
        await asyncio.wait_for(fake.tick.wait(), timeout=1.0)
        fake.tick.clear()
        await asyncio.wait_for(fake.tick.wait(), timeout=1.0)
    finally:
        await sweeper.stop(task)

    assert task.done()
    # The task exited cleanly — ``stop`` does not re-raise
    # CancelledError. ``task.result()`` returns ``None`` for a
    # normally-returning coroutine.
    assert task.cancelled() or task.result() is None
    # We saw at least two ticks.
    assert len(fake.calls) >= 2


@pytest.mark.asyncio
async def test_timeout_sweeper_stop_is_idempotent_on_finished_task() -> None:
    """``stop`` on an already-finished task is a no-op."""
    fake = _FakeGameService()
    sweeper = TimeoutSweeper(fake, interval_seconds=1)  # type: ignore[arg-type]
    sweeper._interval_seconds = 0.01  # type: ignore[assignment]

    task = await sweeper.start()
    # Let a tick happen and then cancel via stop.
    await asyncio.wait_for(fake.tick.wait(), timeout=1.0)
    await sweeper.stop(task)
    # Second stop on the same, already-done task must not raise.
    await sweeper.stop(task)


def test_timeout_sweeper_rejects_non_positive_interval() -> None:
    """Construction fails fast on a zero or negative interval."""

    class _Dummy:
        def sweep_timeouts(self, now=None):  # pragma: no cover - unused
            return []

    with pytest.raises(ValueError):
        TimeoutSweeper(_Dummy(), interval_seconds=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TimeoutSweeper(_Dummy(), interval_seconds=-5)  # type: ignore[arg-type]
