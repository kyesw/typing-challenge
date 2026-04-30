"""Property-based metamorphic test for leaderboard update (task 6.5).

**Property 11: Leaderboard update reflects new Scores (metamorphic).**

**Validates: Requirement 5.6.**

For any Game that transitions to ``completed``, the snapshot
returned by :meth:`LeaderboardService.build_snapshot` after the new
Score is persisted SHALL include a ``LeaderboardEntry`` for that
Player whose ``best_points``, ``best_wpm``, and ``best_accuracy``
are at least the values of the newly persisted Score.

The test encodes this as a classic metamorphic property:

1. Build an initial leaderboard from a generated history of Scores.
2. Persist exactly one additional Score for a chosen Player.
3. Rebuild the leaderboard and compare:

   - **Post-insert monotonicity.** For a Player who already had an
     entry, the new ``best_points``/``best_wpm``/``best_accuracy``
     must each be ``>=`` the old values. Leaderboard maxima never
     regress on insert.
   - **Lower bound from the new Score.** The new entry's
     ``best_points``/``best_wpm``/``best_accuracy`` must each be
     ``>=`` the new Score's corresponding value. This is the
     literal statement of Property 11.
   - **Strict-improvement equality.** If the new Score's ``points``
     strictly exceeds the Player's old ``best_points``, the new
     entry's ``best_points`` equals ``new_points`` exactly.
   - **First-score case.** If the Player had no prior entry, the
     new entry exists and mirrors the new Score's values.

Fresh in-memory SQLite per ``@given`` example keeps the property
stateless.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    Score,
    init_db,
)
from app.services import LeaderboardEntry, LeaderboardService


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "leaderboard-metamorphic",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("leaderboard-metamorphic")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_PLAYER_POOL_SIZE = 6

_BASE_TIME = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Engine / seeding helpers
# ---------------------------------------------------------------------------


def _build_engine() -> Engine:
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


def _seed_players_and_prompt(
    session_factory: sessionmaker[Session],
) -> tuple[list[str], str]:
    player_ids: list[str] = []
    prompt_id = str(uuid.uuid4())

    with session_factory() as s:
        s.add(
            Prompt(
                id=prompt_id,
                text="a" * 120,
                difficulty=None,
                language="en",
            )
        )
        for i in range(_PLAYER_POOL_SIZE):
            player_id = str(uuid.uuid4())
            nickname = f"player_{i:02d}"
            s.add(
                Player(
                    id=player_id,
                    nickname=nickname,
                    nickname_ci=nickname.lower(),
                    session_token=str(uuid.uuid4()),
                    session_expires_at=_BASE_TIME + timedelta(days=365),
                )
            )
            player_ids.append(player_id)
        s.commit()

    return player_ids, prompt_id


def _insert_score(
    session: Session,
    *,
    player_id: str,
    prompt_id: str,
    points: int,
    wpm: float,
    accuracy: float,
    created_at: datetime,
) -> None:
    game_id = str(uuid.uuid4())
    score_id = str(uuid.uuid4())
    session.add(
        Game(
            id=game_id,
            player_id=player_id,
            prompt_id=prompt_id,
            status=GameStatus.COMPLETED,
            started_at=created_at - timedelta(seconds=2),
            ended_at=created_at - timedelta(seconds=1),
        )
    )
    session.add(
        Score(
            id=score_id,
            game_id=game_id,
            player_id=player_id,
            wpm=wpm,
            accuracy=accuracy,
            points=points,
            created_at=created_at,
        )
    )


def _entry_for(
    entries: list[LeaderboardEntry], player_id: str
) -> LeaderboardEntry | None:
    for e in entries:
        if e.player_id == player_id:
            return e
    return None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# History tuple: ``(player_idx, wpm, accuracy, points, offset_seconds)``.
_history_tuple_strategy = st.tuples(
    st.integers(min_value=0, max_value=_PLAYER_POOL_SIZE - 1),
    st.floats(
        min_value=0.0,
        max_value=200.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.floats(
        min_value=0.0,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.integers(min_value=0, max_value=1000),
    st.integers(min_value=0, max_value=43_200),  # 0..12h → pre-insert window
)

# The history list MAY be empty so the "first score for this player"
# branch of Property 11 is exercised.
_history_list_strategy = st.lists(
    _history_tuple_strategy,
    min_size=0,
    max_size=20,
)

# The post-insert Score. Ranges overlap with the history so the new
# score can be strictly better, strictly worse, or tied against the
# player's prior max — each branch exercises a different clause of
# Property 11.
_new_score_strategy = st.tuples(
    st.integers(min_value=0, max_value=_PLAYER_POOL_SIZE - 1),  # player_idx
    st.floats(
        min_value=0.0,
        max_value=200.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # wpm
    st.floats(
        min_value=0.0,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # accuracy
    st.integers(min_value=0, max_value=1000),  # points
)


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


@given(
    history=_history_list_strategy,
    new_score=_new_score_strategy,
)
@settings(max_examples=30, deadline=None)
def test_leaderboard_update_reflects_new_scores(
    history: list[tuple[int, float, float, int, int]],
    new_score: tuple[int, float, float, int],
) -> None:
    """Property 11: snapshot after insert reflects the new Score.

    Validates Requirement 5.6.
    """
    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_ids, prompt_id = _seed_players_and_prompt(session_factory)

    # --- Step 1: seed the history and take the "before" snapshot. ---
    with session_factory() as s:
        for player_idx, wpm, accuracy, points, offset_seconds in history:
            _insert_score(
                s,
                player_id=player_ids[player_idx],
                prompt_id=prompt_id,
                points=points,
                wpm=wpm,
                accuracy=accuracy,
                created_at=_BASE_TIME + timedelta(seconds=offset_seconds),
            )
        s.commit()

    service = LeaderboardService(session_factory)
    before_entries = service.build_snapshot().entries

    # --- Step 2: insert one additional Score for the chosen player. ---
    new_player_idx, new_wpm, new_accuracy, new_points = new_score
    new_player_id = player_ids[new_player_idx]

    # Pick a ``created_at`` strictly after every history timestamp so
    # this score's ``first_best_at`` never wins the tie-break on the
    # prior best unless the new score is a strict-points improvement.
    # That keeps the ``first_best_at`` semantics orthogonal to the
    # monotonicity check; the property we care about is on the
    # numeric maxima, not the tie-break timestamp.
    new_created_at = _BASE_TIME + timedelta(days=1)

    with session_factory() as s:
        _insert_score(
            s,
            player_id=new_player_id,
            prompt_id=prompt_id,
            points=new_points,
            wpm=new_wpm,
            accuracy=new_accuracy,
            created_at=new_created_at,
        )
        s.commit()

    after_entries = service.build_snapshot().entries
    before = _entry_for(before_entries, new_player_id)
    after = _entry_for(after_entries, new_player_id)

    # The new entry must exist in the post-insert snapshot — the
    # player just persisted a completed Score, so Requirement 5.1
    # requires them to be represented.
    assert after is not None, (
        f"no entry for player {new_player_id!r} after persisting a new Score"
    )

    # --- Property 11 clause: new entry is at least the new Score. ---
    assert after.best_points >= new_points, (
        f"best_points regressed below the new Score's points: "
        f"best_points={after.best_points}, new_points={new_points}"
    )
    assert after.best_wpm >= new_wpm, (
        f"best_wpm regressed below the new Score's wpm: "
        f"best_wpm={after.best_wpm}, new_wpm={new_wpm}"
    )
    assert after.best_accuracy >= new_accuracy, (
        f"best_accuracy regressed below the new Score's accuracy: "
        f"best_accuracy={after.best_accuracy}, new_accuracy={new_accuracy}"
    )

    if before is None:
        # First-score-for-this-player case: the only score for the
        # player is the one we just inserted, so the entry mirrors
        # the new Score exactly.
        assert after.best_points == new_points
        assert after.best_wpm == new_wpm
        assert after.best_accuracy == new_accuracy
    else:
        # Post-insert monotonicity: maxima never regress.
        assert after.best_points >= before.best_points, (
            f"best_points regressed across insert for {new_player_id!r}: "
            f"{before.best_points} -> {after.best_points}"
        )
        assert after.best_wpm >= before.best_wpm, (
            f"best_wpm regressed across insert for {new_player_id!r}: "
            f"{before.best_wpm} -> {after.best_wpm}"
        )
        assert after.best_accuracy >= before.best_accuracy, (
            f"best_accuracy regressed across insert for {new_player_id!r}: "
            f"{before.best_accuracy} -> {after.best_accuracy}"
        )

        # Strict-improvement equality: when the new score's points
        # beats the old best, the new ``best_points`` is exactly
        # ``new_points``.
        if new_points > before.best_points:
            assert after.best_points == new_points, (
                "strict improvement on points should set best_points to "
                f"new_points exactly; got best_points={after.best_points}, "
                f"new_points={new_points}"
            )
