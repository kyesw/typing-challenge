"""Property-based test for server-authoritative timing (task 5.6).

**Property 7: Server-authoritative timing.**

**Validates: Requirements 3.6, 15.1, 15.2.**

For any result submission, the Scoring_Service's computed Score
depends only on:

1. The typed text,
2. The Game's prompt, and
3. The server-measured elapsed time ``ended_at - started_at``.

The Score is invariant under any change to the client-supplied
elapsed value. In this codebase the service API
(:meth:`GameService.complete`) does not even accept a client-supplied
elapsed parameter — that is the strongest possible enforcement of
Property 7 — so the property is structurally true by construction.
The test exercises it empirically all the same:

- It generates arbitrary ``client_elapsed`` values and never passes
  them anywhere. Doing so encodes the "invariant under any change to
  the client-supplied elapsed" clause concretely: whatever the client
  thinks, the service's Score does not change.
- It pins the server clock so ``(started_at, ended_at)`` is fixed and
  asserts the persisted ``Score.wpm``/``accuracy``/``points`` match
  the pure-function computation against the server-measured elapsed.
- It also cross-checks two independent Games submitted with identical
  ``(typed_text, prompt, server_elapsed)``: both produce identical
  Scores, which is the symmetric half of the invariance statement
  (if the Score depended on a hidden client value the two calls
  could diverge).

Fast in-memory SQLite is used per ``@given`` example so the test
operates through the real service + ORM path rather than a mock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
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
    GameService,
    ScoringService,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "server-authoritative-timing",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("server-authoritative-timing")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Prompt used by all generated games. Held constant so the property
#: isolates the effect of elapsed time — the typed text varies, the
#: prompt does not. Length is within the ``[100, 500]`` validator
#: range (Requirement 11.3) so real seed prompts exercise the same
#: pure scoring helpers.
_PROMPT_TEXT = (
    "the quick brown fox jumps over the lazy dog and then keeps running "
    "through the quiet forest until it finds a small shaded clearing "
    "where it rests for a while before continuing on its long journey."
)

#: Maximum game duration for the property. Set well above any
#: ``server_elapsed`` generated below so the happy path is exercised
#: (not the timeout branch — that is the domain of Property 14 /
#: task 4.8).
_MAX_DURATION_SECONDS = 10_000


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build_engine() -> Engine:
    """Fresh in-memory SQLite engine per ``@given`` example.

    A per-example engine keeps the property stateless: earlier
    examples cannot leak rows into later ones. SQLite's in-memory
    store is cheap enough that this does not dominate runtime.
    """
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
    prompt_text: str = _PROMPT_TEXT,
) -> tuple[str, str, str]:
    """Insert a Player, a Prompt, and an ``in_progress`` Game.

    Returns ``(player_id, prompt_id, game_id)``. The nickname is unique
    per call so multiple games seeded in one test do not collide on
    ``players.nickname_ci``.
    """
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
                text=prompt_text,
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

    return player_id, prompt_id, game_id


def _build_service(
    session_factory: sessionmaker[Session],
    *,
    clock_value: datetime,
) -> GameService:
    """Construct a ``GameService`` wired to a fixed clock.

    The clock returns ``clock_value`` every call, so ``ended_at`` is
    deterministic inside ``complete``.
    """

    def clock() -> datetime:
        return clock_value

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
#
# ``typed_text`` is drawn from a strategy that often overlaps with
# ``_PROMPT_TEXT`` so accuracy is non-trivial (generating arbitrary
# Unicode would almost always yield accuracy == 0 and make the
# property degenerate). We achieve the overlap by biasing between:
#   * Random slices of the prompt (partial typing — common case).
#   * The full prompt (perfect typing — sanity path).
#   * Random text (low-accuracy path).
#
# ``server_elapsed_seconds`` covers the realistic submission window,
# bounded above by the pinned max_duration so the happy branch runs.
# Lower bound is slightly above zero so ``compute_wpm`` does not
# clamp to the degenerate zero-elapsed case on every example.
#
# ``client_elapsed_seconds`` is generated but never passed to the
# service — it encodes the "client-supplied value" that Property 7
# claims is ignored.


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

_server_elapsed_strategy = st.floats(
    min_value=0.5,
    max_value=float(_MAX_DURATION_SECONDS - 1),
    allow_nan=False,
    allow_infinity=False,
)

_client_elapsed_strategy = st.floats(
    # Include obviously-wrong client values: negatives, enormous
    # overshoots, and sub-millisecond fractions. None of these should
    # affect the persisted Score.
    min_value=-1e6,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


@given(
    typed_text=_typed_text_strategy,
    server_elapsed_seconds=_server_elapsed_strategy,
    client_elapsed_seconds=_client_elapsed_strategy,
)
@settings(max_examples=50, deadline=None)
def test_score_depends_only_on_server_elapsed_typed_and_prompt(
    typed_text: str,
    server_elapsed_seconds: float,
    client_elapsed_seconds: float,
) -> None:
    """Property 7: Score is invariant under client-supplied elapsed.

    Concretely, for any submission:

    * The persisted ``Score.wpm``/``accuracy``/``points`` equal the
      pure-function computation over ``(typed_text, prompt,
      server_elapsed)``.
    * Two independent Games with the same ``(typed_text,
      server_elapsed)`` produce identical Scores, regardless of how
      the (conceptual) client-supplied elapsed differs between them.

    The ``client_elapsed_seconds`` Hypothesis draw is intentionally
    **not passed** to the service. Its presence in the parameter list
    encodes the universal quantifier in "invariant under any change
    to the client-supplied elapsed": whatever the client reports,
    the Score does not move.

    Validates: Requirements 3.6, 15.1, 15.2.
    """
    # The second "client elapsed" is unused on purpose — referenced
    # here so static analysis does not flag it as dead. The property
    # is "the Score is the same regardless of what the client said",
    # which is exactly captured by the service never reading it.
    _ = client_elapsed_seconds

    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=server_elapsed_seconds)

    # --- Game A: first submission. ---
    engine_a = _build_engine()
    session_factory_a = _build_session_factory(engine_a)
    player_a, _, game_a = _seed_player_prompt_game(
        session_factory_a, started_at=started_at
    )
    service_a = _build_service(session_factory_a, clock_value=ended_at)

    result_a = service_a.complete(game_a, typed_text, player_id=player_a)
    assert isinstance(result_a, CompleteGameSuccess), (
        f"expected CompleteGameSuccess, got {type(result_a).__name__}"
    )

    # The service-measured elapsed must match the clock delta to
    # within the resolution of ``timedelta`` (microseconds). If it
    # didn't, a bug in the service would make this test not actually
    # exercise Property 7.
    assert result_a.elapsed_seconds == pytest.approx(
        server_elapsed_seconds, abs=1e-6
    )

    # Compare against the pure-function computation over the SERVER
    # elapsed only. We use ``result_a.elapsed_seconds`` rather than
    # ``server_elapsed_seconds`` because ``timedelta`` stores only
    # microseconds — so the server-measured elapsed is the input that
    # the pure scoring helper actually saw through ``compute_wpm``
    # inside the service. Using it here preserves the property
    # ("Score depends only on server-measured elapsed") without
    # introducing spurious FP noise from sub-microsecond generator
    # draws.
    server_elapsed = result_a.elapsed_seconds
    expected_wpm = compute_wpm(typed_text, _PROMPT_TEXT, server_elapsed)
    expected_accuracy = compute_accuracy(typed_text, _PROMPT_TEXT)
    expected_points = compute_points(expected_wpm, expected_accuracy)
    assert result_a.wpm == expected_wpm
    assert result_a.accuracy == expected_accuracy
    assert result_a.points == expected_points

    # Persisted row mirrors the service's returned payload.
    with session_factory_a() as s:
        row_a = s.execute(
            select(Score).where(Score.game_id == game_a)
        ).scalar_one()
        assert row_a.wpm == expected_wpm
        assert row_a.accuracy == expected_accuracy
        assert row_a.points == expected_points

    # --- Game B: independent submission with identical server inputs. ---
    # Any hidden dependency on a non-server value (e.g., a randomly
    # generated id, a wall clock, or a non-deterministic path) would
    # show up as a mismatch between A's and B's scores.
    engine_b = _build_engine()
    session_factory_b = _build_session_factory(engine_b)
    player_b, _, game_b = _seed_player_prompt_game(
        session_factory_b, started_at=started_at
    )
    service_b = _build_service(session_factory_b, clock_value=ended_at)

    result_b = service_b.complete(game_b, typed_text, player_id=player_b)
    assert isinstance(result_b, CompleteGameSuccess)

    assert result_b.wpm == result_a.wpm
    assert result_b.accuracy == result_a.accuracy
    assert result_b.points == result_a.points
    assert result_b.elapsed_seconds == result_a.elapsed_seconds
