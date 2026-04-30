"""Property-based test for leaderboard aggregation invariant (task 6.3).

**Property 9: Leaderboard aggregation invariant.**

**Validates: Requirements 5.1, 5.2.**

For any set of persisted Scores, the derived Leaderboard:

1. Contains exactly one ``LeaderboardEntry`` per Player who has at
   least one Score (Requirement 5.1).
2. Sets each entry's ``best_points``, ``best_wpm``, and
   ``best_accuracy`` to the maxima of the corresponding values
   across that Player's Scores (Requirement 5.2).

The property treats the service as a black box: we seed a fixed
pool of Players, generate an arbitrary list of
``(player_idx, wpm, accuracy, points, created_at_offset)`` score
tuples, write them through the ORM, call ``build_snapshot()``, and
then compare the entries against the per-player maxima computed
directly from the generated input.

A fresh in-memory SQLite engine is created per ``@given`` example so
the property is stateless across examples. SQLite in-memory is cheap
enough to make this per-example strategy practical at
``max_examples=30``.
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
from app.services import LeaderboardService


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "leaderboard-aggregation",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("leaderboard-aggregation")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Fixed pool size of players — bounded so the strategy produces a
#: mix of scores per player (some with 0, most with >= 1).
_PLAYER_POOL_SIZE = 6

#: Base timestamp used as the origin for generated ``created_at``
#: offsets. Choosing a fixed, timezone-aware base keeps the test
#: reproducible and sidesteps wall-clock drift.
_BASE_TIME = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Engine / seeding helpers
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


def _seed_players_and_prompt(
    session_factory: sessionmaker[Session],
) -> tuple[list[str], str]:
    """Insert ``_PLAYER_POOL_SIZE`` Players and a single throwaway Prompt.

    Returns ``(player_ids, prompt_id)``. Nicknames are generated from
    the player index so they are unique under ``nickname_ci``.
    """
    player_ids: list[str] = []
    prompt_id = str(uuid.uuid4())

    with session_factory() as s:
        s.add(
            Prompt(
                id=prompt_id,
                # Length is well within the validator's [100, 500]
                # range; the leaderboard query doesn't look at
                # prompt text, this is just to satisfy the FK.
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
    """Insert a completed Game + its Score at ``created_at``.

    The ORM models enforce ``ended_at > started_at`` and uniqueness on
    ``scores.game_id``; we pick timestamps just before ``created_at``
    to satisfy both without colliding with other scores for the same
    player.
    """
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


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# A single Score tuple. Ranges are narrow on purpose so the test
# stays focused on the aggregation invariant rather than numeric
# edge cases of the underlying columns.
#
# ``created_at_offset_seconds`` stays below one day so the resulting
# timestamps are unambiguously ordered; the aggregation property
# doesn't depend on the absolute value.
_score_tuple_strategy = st.tuples(
    st.integers(min_value=0, max_value=_PLAYER_POOL_SIZE - 1),  # player_idx
    st.floats(
        min_value=0.0,
        max_value=500.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # wpm
    st.floats(
        min_value=0.0,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # accuracy
    st.integers(min_value=0, max_value=10_000),  # points
    st.integers(min_value=0, max_value=86_400),  # created_at offset seconds
)

# A list of 1..30 score tuples per example. Lower bound of 1 ensures
# the snapshot always has at least one entry to check.
_score_list_strategy = st.lists(
    _score_tuple_strategy,
    min_size=1,
    max_size=30,
)


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


@given(score_specs=_score_list_strategy)
@settings(max_examples=30, deadline=None)
def test_leaderboard_aggregation_invariant(
    score_specs: list[tuple[int, float, float, int, int]],
) -> None:
    """Property 9: one entry per player, each field is the per-player max.

    Validates Requirements 5.1, 5.2.
    """
    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_ids, prompt_id = _seed_players_and_prompt(session_factory)

    # Persist all generated scores and, in parallel, build the
    # reference per-player maxima from the same input data.
    # ``reference[player_id]`` -> ``(max_points, max_wpm, max_accuracy)``.
    reference: dict[str, tuple[int, float, float]] = {}

    with session_factory() as s:
        for player_idx, wpm, accuracy, points, offset_seconds in score_specs:
            player_id = player_ids[player_idx]
            created_at = _BASE_TIME + timedelta(seconds=offset_seconds)

            _insert_score(
                s,
                player_id=player_id,
                prompt_id=prompt_id,
                points=points,
                wpm=wpm,
                accuracy=accuracy,
                created_at=created_at,
            )

            existing = reference.get(player_id)
            if existing is None:
                reference[player_id] = (points, wpm, accuracy)
            else:
                reference[player_id] = (
                    max(existing[0], points),
                    max(existing[1], wpm),
                    max(existing[2], accuracy),
                )
        s.commit()

    # Act: compute the snapshot.
    snapshot = LeaderboardService(session_factory).build_snapshot()

    # --- Requirement 5.1: exactly one entry per player with >= 1 score. ---
    entries_by_player = {e.player_id: e for e in snapshot.entries}
    assert len(entries_by_player) == len(snapshot.entries), (
        "duplicate player_id in leaderboard entries — Requirement 5.1 "
        "requires exactly one entry per Player"
    )
    assert set(entries_by_player.keys()) == set(reference.keys()), (
        "leaderboard players do not match the set of players with >= 1 "
        "score: missing="
        f"{set(reference.keys()) - set(entries_by_player.keys())!r}, "
        f"extra={set(entries_by_player.keys()) - set(reference.keys())!r}"
    )

    # --- Requirement 5.2: per-field maxima. ---
    for player_id, (exp_points, exp_wpm, exp_accuracy) in reference.items():
        entry = entries_by_player[player_id]
        assert entry.best_points == exp_points, (
            f"best_points mismatch for {player_id!r}: "
            f"expected {exp_points}, got {entry.best_points}"
        )
        assert entry.best_wpm == exp_wpm, (
            f"best_wpm mismatch for {player_id!r}: "
            f"expected {exp_wpm}, got {entry.best_wpm}"
        )
        assert entry.best_accuracy == exp_accuracy, (
            f"best_accuracy mismatch for {player_id!r}: "
            f"expected {exp_accuracy}, got {entry.best_accuracy}"
        )
