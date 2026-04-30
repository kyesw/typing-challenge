"""Property-based test for at-most-one in-progress game per player (task 4.7).

**Property 13: At most one in-progress game per player.**

**Validates: Requirements 2.6, 8.6.**

For a single fixed player, the per-player invariant

    count(games where status == in_progress AND player_id == P) <= 1

SHALL hold at every instant, and ``create_game`` SHALL reject any
attempt to open a new Game while the player already has a live one
(``pending`` or ``in_progress``) by returning a
:class:`GameAlreadyInProgress` whose ``game_id`` is the existing
Game's id.

The property is driven by a randomly generated interleaving of:

* ``create`` — call :meth:`GameService.create_game`.
* ``begin`` — call :meth:`GameService.begin_typing` on the current
  live Game id (no-op if the player has no live Game).
* ``submit`` — call :meth:`GameService.complete` on the current live
  Game id with a short typed-text (no-op if the player has no live
  Game). The clock optionally advances before the call so some
  submissions land past ``max_game_duration_seconds`` and drive the
  ``in_progress → abandoned`` branch.
* ``sweep`` — call :meth:`GameService.sweep_timeouts` with the
  current controllable clock. Exercises the parallel abandonment
  path that :meth:`GameService.complete` is NOT the only writer of.

After every action the test queries the DB directly and asserts:

1. At most one Game is in status ``in_progress`` for the player.
2. At most one Game is in a "live" status (``pending`` or
   ``in_progress``) for the player — this is the stricter invariant
   that :meth:`GameService.create_game` actually enforces and that
   Requirement 2.6 / design Error Scenario 3 require.
3. Any :class:`GameAlreadyInProgress` the service returns carries
   the id of the player's (unique) live Game.

Strategy:

- Each ``@given`` example seeds a fresh in-memory SQLite database,
  one Player, and a handful of prompts so the Prompt_Repository
  always has stock.
- Actions are drawn as an arbitrary-length sequence
  (0..25 entries) of :class:`_Action` values. Each action carries
  what it needs (typed-text seed, clock-advance seconds) so the
  top-level ``@given`` composite can pre-materialize the sequence
  without having to read DB state inside the strategy.
- A mutable clock is threaded into the service so ``submit`` and
  ``sweep`` can deterministically cross the
  ``max_game_duration_seconds`` boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

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
    init_db,
)
from app.services import (
    CompleteGameSuccess,
    CompleteGameTimeout,
    CreateGameSuccess,
    GameAlreadyInProgress,
    GameNotFound,
    GameNotInPending,
    GameNotInProgress,
    GameService,
    ScoringService,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "at-most-one-in-progress",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("at-most-one-in-progress")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: A well-formed prompt body (length within [100, 500] per Requirement
#: 11.3) used across all prompts seeded for the property. Kept constant
#: so the test focuses on the per-player lifecycle invariant rather
#: than on prompt selection.
_PROMPT_TEXT = (
    "the quick brown fox jumps over the lazy dog and then keeps running "
    "through the quiet forest until it finds a small shaded clearing "
    "where it rests for a while before continuing on its long journey."
)

#: Maximum game duration for the pinned ``Settings``. Kept short so
#: ``submit`` and ``sweep`` can cross the threshold reliably within a
#: bounded number of clock advances.
_MAX_DURATION_SECONDS = 60

#: Number of prompts to seed. ``GameService.create_game`` calls
#: :meth:`PromptRepository.select_prompt` once per creation; the
#: repository's default is random selection, so having several prompts
#: in the pool is harmless and matches production. A single prompt
#: would also work.
_SEED_PROMPT_COUNT = 3


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


class _Kind(str, Enum):
    CREATE = "create"
    BEGIN = "begin"
    SUBMIT = "submit"
    SWEEP = "sweep"


@dataclass(frozen=True)
class _Action:
    """A single action to apply to the service.

    Attributes:
        kind: Which service method to exercise.
        typed_text: Text to pass to :meth:`GameService.complete`.
            Ignored for other kinds. Kept small so examples shrink
            cleanly; content is irrelevant to Property 13.
        advance_seconds: How far to advance the mutable clock
            *before* the action fires. Non-negative. Allows ``submit``
            and ``sweep`` to cross the ``_MAX_DURATION_SECONDS``
            boundary deterministically.
    """

    kind: _Kind
    typed_text: str
    advance_seconds: float


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


def _seed_player_and_prompts(
    session_factory: sessionmaker[Session],
) -> str:
    """Insert one Player and a few Prompts. Return the player id."""
    player_id = str(uuid.uuid4())
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
        for _ in range(_SEED_PROMPT_COUNT):
            s.add(
                Prompt(
                    id=str(uuid.uuid4()),
                    text=_PROMPT_TEXT,
                    difficulty=None,
                    language="en",
                )
            )
        s.commit()
    return player_id


class _MutableClock:
    """Minimal mutable UTC clock threaded into the service.

    The :class:`GameService` reads the clock once per public call; the
    test advances it between calls via :meth:`advance` to drive the
    service across the ``_MAX_DURATION_SECONDS`` boundary as needed.
    """

    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds <= 0:
            return
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


def _count_live_and_in_progress(
    session_factory: sessionmaker[Session],
    player_id: str,
) -> tuple[int, int, list[str]]:
    """Return (live_count, in_progress_count, live_ids) for the player.

    "Live" means PENDING or IN_PROGRESS — the two non-terminal
    statuses. The list of live ids lets the caller match a returned
    :class:`GameAlreadyInProgress` payload against reality.
    """
    with session_factory() as s:
        rows = (
            s.execute(
                select(Game.id, Game.status).where(Game.player_id == player_id)
            )
            .all()
        )
    live_ids: list[str] = []
    in_progress = 0
    for gid, status in rows:
        if status is GameStatus.IN_PROGRESS:
            in_progress += 1
            live_ids.append(gid)
        elif status is GameStatus.PENDING:
            live_ids.append(gid)
    return len(live_ids), in_progress, live_ids


def _current_live_game_id(
    session_factory: sessionmaker[Session],
    player_id: str,
) -> str | None:
    """The one live Game's id, if any — used to drive begin/submit."""
    _, _, live_ids = _count_live_and_in_progress(session_factory, player_id)
    # Invariant enforced downstream; for the purpose of picking an
    # action target we tolerate the (illegal) multi-live case by
    # simply choosing the first. The assertions will still fire on
    # the invariant check after the action.
    return live_ids[0] if live_ids else None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_kind_strategy = st.sampled_from(list(_Kind))

#: Small typed-text strategy. Content is irrelevant to Property 13;
#: the key is that something plausible reaches
#: :meth:`GameService.complete` without overflowing any length guard.
_typed_text_strategy = st.text(min_size=0, max_size=40)

#: Clock-advance strategy. Upper bound deliberately lets the clock
#: race past ``_MAX_DURATION_SECONDS`` so the timeout branch in
#: :meth:`GameService.complete` and the sweep path are both exercised
#: in some examples. ``min_value=0.0`` keeps the mostly-pure
#: interleaving case covered too.
_advance_seconds_strategy = st.floats(
    min_value=0.0,
    max_value=float(_MAX_DURATION_SECONDS) * 2.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _action(draw: st.DrawFn) -> _Action:
    return _Action(
        kind=draw(_kind_strategy),
        typed_text=draw(_typed_text_strategy),
        advance_seconds=draw(_advance_seconds_strategy),
    )


_actions_strategy = st.lists(_action(), min_size=0, max_size=25)


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


@given(actions=_actions_strategy)
@settings(max_examples=30, deadline=None)
def test_at_most_one_in_progress_game_per_player(
    actions: list[_Action],
) -> None:
    """Property 13: at most one in-progress (and one live) game per player.

    For the fixed single player seeded at the start of each example,
    after every action the test asserts:

    * The count of Games with ``status == in_progress`` is ``<= 1``
      (Requirement 8.6).
    * The count of Games with ``status in {pending, in_progress}``
      is ``<= 1`` (the stricter invariant :meth:`create_game`
      enforces; Requirement 2.6 / design Error Scenario 3).
    * Any :class:`GameAlreadyInProgress` returned by
      :meth:`create_game` carries the id of the player's current
      live Game.
    """
    # ------------------------------------------------------------------
    # Setup.
    # ------------------------------------------------------------------
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_id = _seed_player_and_prompts(session_factory)
    clock = _MutableClock(t0)
    service = _build_service(session_factory, clock=clock)

    # ------------------------------------------------------------------
    # Baseline: fresh player has zero live games.
    # ------------------------------------------------------------------
    live, in_progress, _ = _count_live_and_in_progress(session_factory, player_id)
    assert live == 0
    assert in_progress == 0

    # ------------------------------------------------------------------
    # Action loop.
    # ------------------------------------------------------------------
    for act in actions:
        clock.advance(act.advance_seconds)

        if act.kind is _Kind.CREATE:
            result = service.create_game(player_id)
            # Every CREATE result must be one of the two documented
            # variants (PlayerNotFound is unreachable given the seed).
            assert isinstance(result, (CreateGameSuccess, GameAlreadyInProgress))
            if isinstance(result, GameAlreadyInProgress):
                # Requirement 2.6: the conflict payload MUST carry
                # the existing live Game's id. Verify against the DB
                # directly rather than trusting the in-memory count.
                _, _, live_ids = _count_live_and_in_progress(
                    session_factory, player_id
                )
                assert result.game_id in live_ids, (
                    f"GameAlreadyInProgress.game_id={result.game_id!r} "
                    f"not in live_ids={live_ids!r}"
                )
                # The returned status must be non-terminal (pending
                # or in_progress) — the service's contract.
                assert result.status in (
                    GameStatus.PENDING,
                    GameStatus.IN_PROGRESS,
                )

        elif act.kind is _Kind.BEGIN:
            target = _current_live_game_id(session_factory, player_id)
            if target is None:
                # No live Game to begin; skip silently — this mirrors
                # what a well-behaved client would do and keeps the
                # interleaving honest. The invariant is still checked
                # below.
                pass
            else:
                result = service.begin_typing(target, player_id=player_id)
                # Must be one of the documented variants.
                assert isinstance(
                    result,
                    (type(result),),  # trivially true; keeps type narrow
                )
                assert result.__class__.__name__ in {
                    "BeginTypingSuccess",
                    "GameNotFound",
                    "GameNotInPending",
                }

        elif act.kind is _Kind.SUBMIT:
            target = _current_live_game_id(session_factory, player_id)
            if target is None:
                pass
            else:
                result = service.complete(
                    target, act.typed_text, player_id=player_id
                )
                # One of the four documented variants — the test
                # doesn't care which, only that the invariant holds
                # after.
                assert isinstance(
                    result,
                    (
                        CompleteGameSuccess,
                        CompleteGameTimeout,
                        GameNotFound,
                        GameNotInProgress,
                    ),
                )

        elif act.kind is _Kind.SWEEP:
            service.sweep_timeouts(now=clock())

        else:  # pragma: no cover - exhaustive enum above
            raise AssertionError(f"unhandled kind {act.kind!r}")

        # ------------------------------------------------------------
        # Invariant: check after EVERY action (Requirement 8.6).
        # ------------------------------------------------------------
        live, in_progress, live_ids = _count_live_and_in_progress(
            session_factory, player_id
        )
        assert in_progress <= 1, (
            f"in_progress count {in_progress} > 1 after {act.kind.value}; "
            f"live_ids={live_ids!r}"
        )
        assert live <= 1, (
            f"live count {live} > 1 after {act.kind.value}; "
            f"live_ids={live_ids!r}"
        )

    # ------------------------------------------------------------------
    # Final invariant re-check (redundant with the loop check, kept
    # as an explicit close-out for clarity).
    # ------------------------------------------------------------------
    live, in_progress, live_ids = _count_live_and_in_progress(
        session_factory, player_id
    )
    assert in_progress <= 1
    assert live <= 1


# ---------------------------------------------------------------------------
# Focused sub-property: conflict payload carries the existing id.
# ---------------------------------------------------------------------------


@given(
    initial_advance=st.floats(
        min_value=0.0,
        max_value=10.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    second_advance=st.floats(
        min_value=0.0,
        max_value=10.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    begin_before_second_create=st.booleans(),
)
@settings(max_examples=30, deadline=None)
def test_create_game_conflict_echoes_existing_live_game_id(
    initial_advance: float,
    second_advance: float,
    begin_before_second_create: bool,
) -> None:
    """Requirement 2.6 in isolation: conflict payload = existing live id.

    Shrinks cleanly: a successful first ``create_game`` produces a
    live Game, an optional ``begin_typing`` advances it into
    ``in_progress``, and the second ``create_game`` must return
    :class:`GameAlreadyInProgress` whose ``game_id`` equals the first
    Game's id.
    """
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_id = _seed_player_and_prompts(session_factory)
    clock = _MutableClock(t0)
    service = _build_service(session_factory, clock=clock)

    clock.advance(initial_advance)
    first = service.create_game(player_id)
    assert isinstance(first, CreateGameSuccess)
    first_id = first.game_id

    if begin_before_second_create:
        begun = service.begin_typing(first_id, player_id=player_id)
        # Must succeed — the Game is pending and owned by the player.
        assert begun.__class__.__name__ == "BeginTypingSuccess"

    clock.advance(second_advance)
    second = service.create_game(player_id)

    assert isinstance(second, GameAlreadyInProgress)
    assert second.game_id == first_id
    # Status must be one of the two non-terminal live statuses.
    assert second.status in (GameStatus.PENDING, GameStatus.IN_PROGRESS)
    # And it must match the DB's view of that Game.
    with session_factory() as s:
        row = s.get(Game, first_id)
        assert row is not None
        assert row.status == second.status
