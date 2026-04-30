"""Property-based test for one-Score-per-completed-Game (task 5.7).

**Property 8: Exactly one Score per completed Game with consistent end state.**

**Validates: Requirements 4.4, 4.5, 8.3, 8.7.**

For any sequence of result submissions against a single Game,
once the Game reaches status ``completed``:

1. Exactly one persisted Score references that ``gameId``.
2. The Game's ``status`` is ``completed``.
3. The Game's ``endedAt > startedAt``.

This property is about write-path idempotency under retries. A
well-meaning client that retries a submission (network loss,
timeout on its side, optimistic double-click) must not produce
two Score rows. The service layer must:

- Accept the first submission and transition the Game to
  ``completed`` with a Score row.
- Reject every subsequent submission with
  :class:`GameNotInProgress` (the Game is no longer ``in_progress``),
  leaving the Score count at one.

The test also holds the weaker, always-true invariant that *at
most* one Score row can ever exist for a given ``gameId``, even if
the first submission times out. The UNIQUE constraint on
``scores.game_id`` is the backstop; the service's own up-front
check is the primary defense.

Strategy:

- Each ``@given`` example seeds a fresh in-memory database so the
  property is stateless across examples.
- A Game is created in ``in_progress`` with a known ``started_at``.
- A list of 1..8 submissions is generated. Each submission is a
  ``(typed_text, advance_seconds)`` pair; after each submission the
  server clock advances by ``advance_seconds``. The first
  submission is pinned to land within the timeout window so the
  happy path is exercised reliably and the "reached completed"
  premise of Property 8 applies. Retries may land before or after
  the timeout cutoff — both are valid submission sequences under
  the acceptance criteria.
- After the sequence runs, the test asserts Property 8 holds.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
    GameNotInProgress,
    GameService,
    ScoringService,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "score-uniqueness",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("score-uniqueness")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Prompt used for every game in the property test. Kept constant and
#: well-formed (length in ``[100, 500]`` per Requirement 11.3) so the
#: property focuses on the submission-sequence invariant rather than
#: on prompt validity.
_PROMPT_TEXT = (
    "the quick brown fox jumps over the lazy dog and then keeps running "
    "through the quiet forest until it finds a small shaded clearing "
    "where it rests for a while before continuing on its long journey."
)

#: Maximum game duration for the property's pinned ``Settings``. Kept
#: generous so the initial submission reliably lands in the valid
#: window; later retries may or may not exceed it depending on the
#: generated ``advance_seconds``.
_MAX_DURATION_SECONDS = 120


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
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def _seed_player_prompt_game(
    session_factory: sessionmaker[Session],
    *,
    started_at: datetime,
) -> tuple[str, str]:
    """Insert a Player + Prompt + in_progress Game. Return (player_id, game_id)."""
    player_id = str(uuid.uuid4())
    prompt_id = str(uuid.uuid4())
    game_id = str(uuid.uuid4())
    nickname = f"p-{player_id[:8]}"

    with session_factory() as s:
        s.add(
            Player(
                id=player_id,
                nickname=nickname,
                nickname_ci=nickname.lower(),
                session_token=str(uuid.uuid4()),
                session_expires_at=datetime.now(timezone.utc)
                + timedelta(hours=1),
            )
        )
        s.add(
            Prompt(
                id=prompt_id,
                text=_PROMPT_TEXT,
                difficulty=None,
                language="en",
            )
        )
        s.add(
            Game(
                id=game_id,
                player_id=player_id,
                prompt_id=prompt_id,
                status=GameStatus.IN_PROGRESS,
                started_at=started_at,
                ended_at=None,
            )
        )
        s.commit()

    return player_id, game_id


class _MutableClock:
    """Minimal mutable clock for driving the service across retries.

    Each call to the clock returns the current value; advance between
    calls via :meth:`advance`. This lets the test script a sequence
    of submissions with controlled ``ended_at`` timestamps without
    having to reach into the service internals.
    """

    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        # timedelta microsecond resolution is fine; the property only
        # needs enough dynamic range to move past the timeout cutoff
        # on some retries.
        self._now = self._now + timedelta(seconds=seconds)


def _build_service(
    session_factory: sessionmaker[Session],
    *,
    clock: _MutableClock,
) -> GameService:
    scoring = ScoringService()
    repo = PromptRepository(session_factory)
    return GameService(
        session_factory,
        repo,
        clock=clock,
        scoring_service=scoring,
        settings=Settings(max_game_duration_seconds=_MAX_DURATION_SECONDS),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Typed-text strategy: biased toward text that is likely to produce
# non-trivial accuracy (slices and full prompt) so the scoring path is
# exercised, but also includes arbitrary text so the property is not
# limited to the perfect-typing branch.
_prompt_slice_strategy = st.builds(
    lambda start, length: _PROMPT_TEXT[start : start + length],
    start=st.integers(min_value=0, max_value=len(_PROMPT_TEXT)),
    length=st.integers(min_value=0, max_value=len(_PROMPT_TEXT)),
)

_typed_text_strategy = st.one_of(
    _prompt_slice_strategy,
    st.just(_PROMPT_TEXT),
    st.text(max_size=len(_PROMPT_TEXT) + 50),
)


# Clock-advance strategy: a submission at or near the end of the
# valid window may or may not time out on subsequent retries. The
# upper bound deliberately lets the clock race past
# ``_MAX_DURATION_SECONDS`` so the timeout branch is also exercised
# in some examples.
_advance_seconds_strategy = st.floats(
    min_value=0.001,
    max_value=float(_MAX_DURATION_SECONDS) * 2.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _submission_sequence(draw: st.DrawFn) -> list[tuple[str, float]]:
    """A sequence of 1..8 submissions.

    The first element's ``advance_seconds`` is bounded below the
    timeout cutoff so the initial submission lands in the valid
    window (and therefore exercises the happy-path transition to
    ``completed``). Subsequent elements have no such bound — they
    test the idempotency of retries after the Game has already been
    finalized, whether by ``completed`` or ``abandoned``.
    """
    # Initial submission: guaranteed inside the timeout window. We
    # leave a small safety margin below ``_MAX_DURATION_SECONDS`` so
    # floating-point noise cannot push the first submission across.
    first_typed = draw(_typed_text_strategy)
    first_advance = draw(
        st.floats(
            min_value=0.001,
            max_value=float(_MAX_DURATION_SECONDS - 1),
            allow_nan=False,
            allow_infinity=False,
        )
    )

    rest = draw(
        st.lists(
            st.tuples(_typed_text_strategy, _advance_seconds_strategy),
            min_size=0,
            max_size=7,
        )
    )
    return [(first_typed, first_advance), *rest]


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


@given(submissions=_submission_sequence())
@settings(max_examples=50, deadline=None)
def test_at_most_one_score_per_game_and_consistent_end_state(
    submissions: list[tuple[str, float]],
) -> None:
    """Property 8: exactly one Score per completed Game + consistent end state.

    For any sequence of submissions against a single Game:

    * At most one Score row exists for that ``gameId`` at any time.
    * If any submission succeeded (``CompleteGameSuccess``), the Game
      is in ``completed`` with exactly one Score and
      ``endedAt > startedAt``.
    * If no submission succeeded (all rejected as timeouts or
      out-of-state), the Game is not in ``completed`` and has zero
      Scores (a sanity check on the retry branch — the write path
      must not leave a half-written row behind).

    Validates: Requirements 4.4, 4.5, 8.3, 8.7.
    """
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_id, game_id = _seed_player_prompt_game(
        session_factory, started_at=started_at
    )

    # The clock starts at started_at; each submission advances it by
    # the generated amount before calling ``complete`` so the
    # service's ``ended_at`` is deterministic for the test.
    clock = _MutableClock(started_at)
    service = _build_service(session_factory, clock=clock)

    any_success = False
    any_timeout = False
    successful_ended_at: datetime | None = None

    for typed_text, advance_seconds in submissions:
        clock.advance(advance_seconds)
        result = service.complete(game_id, typed_text, player_id=player_id)

        # After each submission, the per-gameId Score count must
        # never exceed 1 — the central invariant of Property 8.
        with session_factory() as s:
            count = len(
                s.execute(
                    select(Score).where(Score.game_id == game_id)
                ).scalars().all()
            )
            assert count <= 1, (
                f"more than one Score row for game {game_id!r}: {count}"
            )

        if isinstance(result, CompleteGameSuccess):
            # The first success transitions the Game to completed;
            # no later call should report success again.
            assert not any_success, (
                "second CompleteGameSuccess for the same game — retries "
                "should not produce duplicate Scores"
            )
            any_success = True
            successful_ended_at = result.ended_at
        elif isinstance(result, CompleteGameTimeout):
            # Timeout is only allowed when the Game has not already
            # been successfully completed. Once completed, a retry
            # must surface as GameNotInProgress, not as a timeout.
            assert not any_success, (
                "timeout after a successful completion — the Game "
                "should already be in ``completed``, not re-evaluated"
            )
            any_timeout = True
        elif isinstance(result, GameNotInProgress):
            # The only reason we'd see this is that the Game was
            # already finalized by a prior submission. If that's the
            # case, the prior submission must have either succeeded
            # or timed out.
            assert any_success or any_timeout, (
                "GameNotInProgress on the first submission — the seed "
                "leaves the Game in_progress, so this shouldn't happen"
            )
        else:
            # GameNotFound would indicate the seed is wrong or the
            # ownership check misbehaved; fail loudly so such a
            # regression cannot hide behind the property.
            raise AssertionError(
                f"unexpected result variant {type(result).__name__}"
            )

    # Post-sequence invariants.
    with session_factory() as s:
        final_game = s.get(Game, game_id)
        assert final_game is not None

        final_scores = (
            s.execute(
                select(Score).where(Score.game_id == game_id)
            ).scalars().all()
        )
        assert len(final_scores) <= 1

        if any_success:
            # The core conjunction of Property 8: reached completed =>
            # exactly one Score + status completed + ended_at > started_at.
            assert len(final_scores) == 1
            assert final_game.status is GameStatus.COMPLETED
            # Normalize for SQLite's naive round-trip of timezone-
            # aware columns so the comparison is well-defined.
            game_started = final_game.started_at
            game_ended = final_game.ended_at
            assert game_started is not None
            assert game_ended is not None
            if game_started.tzinfo is None:
                game_started = game_started.replace(tzinfo=timezone.utc)
            if game_ended.tzinfo is None:
                game_ended = game_ended.replace(tzinfo=timezone.utc)
            assert game_ended > game_started, (
                f"ended_at ({game_ended!r}) must be strictly after "
                f"started_at ({game_started!r})"
            )
            # The successful submission's ``ended_at`` is the one
            # that was persisted onto the Game row.
            assert successful_ended_at is not None
            if successful_ended_at.tzinfo is None:  # pragma: no cover
                successful_ended_at = successful_ended_at.replace(
                    tzinfo=timezone.utc
                )
            assert game_ended == successful_ended_at
        else:
            # No success means either all submissions timed out or
            # the Game was never finalized. Either way, there must be
            # zero Score rows and the Game must not be ``completed``.
            assert len(final_scores) == 0
            assert final_game.status is not GameStatus.COMPLETED
