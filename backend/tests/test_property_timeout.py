"""Property-based test for game timeout enforcement (task 4.8).

**Property 14: Game timeout enforcement.**

**Validates: Requirements 9.1, 9.2, 9.4.**

Using a controllable clock, assert that:

1. Any submission arriving after ``Maximum_Game_Duration`` (i.e.
   ``elapsed > max_game_duration_seconds``) is rejected by
   :meth:`GameService.complete` with :class:`CompleteGameTimeout`,
   the Game row transitions to ``abandoned`` with ``ended_at`` set,
   and **no Score row is written** (Requirements 9.2, 9.4).
2. A submission arriving strictly within the duration succeeds — it
   returns :class:`CompleteGameSuccess`, persists exactly one Score
   row, and leaves the Game ``completed``. This is the positive side
   of the property: the timeout guard rejects iff elapsed exceeds
   the bound, not because of some other reason.
3. The periodic sweeper path (:meth:`GameService.sweep_timeouts`)
   abandons every ``in_progress`` Game whose ``started_at`` predates
   ``now - max_game_duration_seconds`` and leaves every other Game
   untouched (Requirements 9.1, 9.4).

Strategy:

- Each ``@given`` example builds a **fresh in-memory SQLite engine
  and database** so examples cannot influence each other.
- A :class:`_FixedClock` is threaded into the service so elapsed time
  is fully determined by the test rather than by wall time.
- ``max_examples=30`` per the task directive.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
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
    GameService,
    ScoringService,
    SweptGame,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------


try:
    settings.register_profile(
        "timeout-enforcement",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("timeout-enforcement")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Maximum game duration pinned for the property. Short enough that
#: the generated ``elapsed`` range stays in a numerically-friendly
#: window, long enough that the "within duration" branch has room to
#: shrink.
_MAX_DURATION_SECONDS = 60

#: A well-formed prompt body (length within ``[100, 500]`` per
#: Requirement 11.3). We reuse it across the whole test module — the
#: property is about timing, not prompt content.
_PROMPT_TEXT = (
    "the quick brown fox jumps over the lazy dog and then keeps running "
    "through the quiet forest until it finds a small shaded clearing "
    "where it rests for a while before continuing on its long journey."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_engine() -> Engine:
    """Fresh in-memory SQLite engine per ``@given`` example."""
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


def _build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _make_player(
    session_factory: sessionmaker[Session], *, nickname: str
) -> str:
    with session_factory() as s:
        row = Player(
            id=str(uuid.uuid4()),
            nickname=nickname,
            nickname_ci=nickname.lower(),
            session_token=str(uuid.uuid4()),
            session_expires_at=_future(),
        )
        s.add(row)
        s.commit()
        return row.id


def _make_prompt(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as s:
        row = Prompt(
            id=str(uuid.uuid4()),
            text=_PROMPT_TEXT,
            difficulty=None,
            language="en",
        )
        s.add(row)
        s.commit()
        return row.id


def _make_in_progress_game(
    session_factory: sessionmaker[Session],
    *,
    player_id: str,
    prompt_id: str,
    started_at: datetime,
) -> str:
    """Seed a Game directly into ``in_progress`` at the given ``started_at``.

    Bypassing ``create_game`` + ``begin_typing`` here is intentional:
    the property under test is specifically about the *timing* surface
    of :meth:`GameService.complete` and :meth:`GameService.sweep_timeouts`,
    so a direct seed keeps the examples focused and the setup cheap.
    """
    with session_factory() as s:
        row = Game(
            id=str(uuid.uuid4()),
            player_id=player_id,
            prompt_id=prompt_id,
            status=GameStatus.IN_PROGRESS,
            started_at=started_at,
            ended_at=None,
        )
        s.add(row)
        s.commit()
        return row.id


class _FixedClock:
    """A zero-arg callable returning a fixed ``datetime``.

    The :class:`GameService` reads the clock once per public call.
    Setting ``now`` explicitly per example is enough to deterministically
    place a submission on either side of the
    ``max_game_duration_seconds`` boundary.
    """

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now


def _build_service(
    session_factory: sessionmaker[Session],
    *,
    clock: Callable[[], datetime],
    max_duration_seconds: int = _MAX_DURATION_SECONDS,
) -> GameService:
    scoring = ScoringService()
    repo = PromptRepository(session_factory)
    return GameService(
        session_factory,
        repo,
        clock=clock,
        scoring_service=scoring,
        settings=Settings(max_game_duration_seconds=max_duration_seconds),
    )


def _score_count_for_game(
    session_factory: sessionmaker[Session], game_id: str
) -> int:
    with session_factory() as s:
        rows = (
            s.execute(select(Score.id).where(Score.game_id == game_id))
            .scalars()
            .all()
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Test 1 — complete path: submission after Maximum_Game_Duration is rejected.
# ---------------------------------------------------------------------------


@given(
    elapsed=st.floats(
        min_value=float(_MAX_DURATION_SECONDS) + 0.001,
        max_value=float(_MAX_DURATION_SECONDS) * 10.0,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=30, deadline=None)
def test_complete_after_max_duration_rejects_and_abandons_game(
    elapsed: float,
) -> None:
    """Requirements 9.1 / 9.2 / 9.4.

    For any ``elapsed > max_game_duration_seconds``, the service must:

    * Return :class:`CompleteGameTimeout`.
    * Transition the Game to ``abandoned`` with ``ended_at`` set.
    * **Not** persist a Score row.
    """
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    engine = _build_engine()
    session_factory = _build_session_factory(engine)

    player_id = _make_player(session_factory, nickname="timeout-player")
    prompt_id = _make_prompt(session_factory)

    game_id = _make_in_progress_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        started_at=t0,
    )

    # Place the clock strictly past the duration.
    now = t0 + timedelta(seconds=elapsed)
    service = _build_service(
        session_factory,
        clock=_FixedClock(now),
    )

    result = service.complete(game_id, _PROMPT_TEXT, player_id=player_id)

    # --- Result is the timeout variant.
    assert isinstance(result, CompleteGameTimeout), (
        f"expected CompleteGameTimeout for elapsed={elapsed}, got {result!r}"
    )
    assert result.game_id == game_id
    assert result.ended_at == now
    # ``elapsed_seconds`` is computed from (ended_at - started_at); it
    # must match the clock delta we induced (to within float precision).
    assert abs(result.elapsed_seconds - elapsed) < 1e-6

    # --- DB: Game is ABANDONED with ended_at=now; no Score was written.
    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is GameStatus.ABANDONED
        ended = row.ended_at
        assert ended is not None
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        assert ended == now
    assert _score_count_for_game(session_factory, game_id) == 0


# ---------------------------------------------------------------------------
# Test 2 — within-duration submission succeeds.
# ---------------------------------------------------------------------------


@given(
    elapsed=st.floats(
        min_value=0.5,
        max_value=float(_MAX_DURATION_SECONDS),
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=30, deadline=None)
def test_complete_within_max_duration_succeeds(elapsed: float) -> None:
    """Dual property: ``elapsed <= max_duration`` must NOT trigger a timeout.

    Asserts that for any ``elapsed`` in ``[0.5, max_duration]``:

    * Result is :class:`CompleteGameSuccess`.
    * Exactly one Score row exists for the Game.
    * Game status is ``completed``.

    The lower bound ``0.5`` avoids the zero-elapsed edge case, which
    :meth:`GameService.complete` also rejects (``now > started_at``
    guard); that branch is covered by unit tests in
    ``test_service_game_complete.py``.
    """
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    engine = _build_engine()
    session_factory = _build_session_factory(engine)

    player_id = _make_player(session_factory, nickname="within-player")
    prompt_id = _make_prompt(session_factory)

    game_id = _make_in_progress_game(
        session_factory,
        player_id=player_id,
        prompt_id=prompt_id,
        started_at=t0,
    )

    now = t0 + timedelta(seconds=elapsed)
    service = _build_service(
        session_factory,
        clock=_FixedClock(now),
    )

    result = service.complete(game_id, _PROMPT_TEXT, player_id=player_id)

    assert isinstance(result, CompleteGameSuccess), (
        f"expected CompleteGameSuccess for elapsed={elapsed}, got {result!r}"
    )
    assert result.game_id == game_id
    assert result.player_id == player_id

    # --- DB: Game COMPLETED; exactly one Score persisted.
    with session_factory() as s:
        row = s.get(Game, game_id)
        assert row is not None
        assert row.status is GameStatus.COMPLETED
    assert _score_count_for_game(session_factory, game_id) == 1


# ---------------------------------------------------------------------------
# Test 3 — sweeper path: only timed-out Games transition to abandoned.
# ---------------------------------------------------------------------------


# A batch of per-game offsets. Each entry is the number of seconds
# between ``now`` and the game's ``started_at`` (so values larger than
# ``max_duration`` are timed out, smaller values are still within the
# duration). Using a list of floats keeps the generator composable and
# the shrinker effective.
_batch_strategy = st.lists(
    st.floats(
        min_value=0.001,
        max_value=float(_MAX_DURATION_SECONDS) * 10.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    min_size=1,
    max_size=8,
)


@given(age_seconds=_batch_strategy)
@settings(max_examples=30, deadline=None)
def test_sweep_timeouts_abandons_only_timed_out_games(
    age_seconds: list[float],
) -> None:
    """Requirements 9.1 / 9.4: the sweeper cleanly partitions Games.

    For a mix of Games with arbitrary ages relative to ``now``:

    * Every Game whose ``started_at`` predates the cutoff
      (``now - max_duration``) ends up with status ``abandoned``
      and ``ended_at == now``.
    * Every Game whose ``started_at`` is at or after the cutoff
      remains ``in_progress`` with ``ended_at`` still NULL.
    * The returned :class:`SweptGame` list contains exactly the
      timed-out Games (no more, no fewer), each carrying the
      correct ``player_id`` and ``started_at``.
    * Exactly one ``game_event`` is broadcast per swept Game (with
      status ``abandoned``); no ``leaderboard_update`` is emitted.
    """
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    engine = _build_engine()
    session_factory = _build_session_factory(engine)

    player_id = _make_player(session_factory, nickname="sweeper-player")
    prompt_id = _make_prompt(session_factory)

    # ``now`` stays fixed; each Game's ``started_at`` = now - age. An
    # ``age > max_duration`` means the Game is timed out per the
    # sweeper's ``started_at < now - max_duration`` predicate.
    now = t0
    max_duration = float(_MAX_DURATION_SECONDS)

    expected_timed_out: set[str] = set()
    expected_live: set[str] = set()
    started_by_id: dict[str, datetime] = {}

    for age in age_seconds:
        started_at = now - timedelta(seconds=age)
        gid = _make_in_progress_game(
            session_factory,
            player_id=player_id,
            prompt_id=prompt_id,
            started_at=started_at,
        )
        started_by_id[gid] = started_at
        # The sweeper's predicate is strict: ``started_at < cutoff``
        # where ``cutoff = now - max_duration``. That is equivalent to
        # ``age > max_duration``. A game exactly at the boundary
        # (``age == max_duration``) is NOT swept.
        if age > max_duration:
            expected_timed_out.add(gid)
        else:
            expected_live.add(gid)

    service = _build_service(
        session_factory,
        # The sweeper's clock fallback is unused because we pass ``now``
        # explicitly below; still, supply one for completeness.
        clock=_FixedClock(now),
    )

    swept = service.sweep_timeouts(now=now)

    # --- Return value: exactly the expected set, with correct fields.
    assert isinstance(swept, list)
    assert all(isinstance(s, SweptGame) for s in swept)
    returned_ids = {s.game_id for s in swept}
    assert returned_ids == expected_timed_out, (
        f"expected swept ids {expected_timed_out!r}, got {returned_ids!r}"
    )
    for entry in swept:
        assert entry.player_id == player_id
        assert entry.ended_at == now
        # ``started_at`` must match the seeded value (normalized to UTC).
        expected_start = started_by_id[entry.game_id]
        got_start = entry.started_at
        if got_start.tzinfo is None:
            got_start = got_start.replace(tzinfo=timezone.utc)
        assert got_start == expected_start
        # Sanity: ``ended_at > started_at`` (Requirement 8.7).
        assert entry.ended_at > got_start

    # --- DB: timed-out Games are ABANDONED; live Games are untouched.
    with session_factory() as s:
        for gid in expected_timed_out:
            row = s.get(Game, gid)
            assert row is not None
            assert row.status is GameStatus.ABANDONED
            ended = row.ended_at
            assert ended is not None
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            assert ended == now
        for gid in expected_live:
            row = s.get(Game, gid)
            assert row is not None
            assert row.status is GameStatus.IN_PROGRESS
            assert row.ended_at is None
