"""Property-based test for leaderboard ordering + rank invariant (task 6.4).

**Property 10: Leaderboard ordering and rank invariant.**

**Validates: Requirements 5.3, 5.4.**

For any set of persisted Scores, the Leaderboard returned by
:meth:`LeaderboardService.build_snapshot` satisfies:

1. Entries are sorted by the composite key
   ``(-best_points, -best_wpm, first_best_at)`` — i.e.
   ``best_points`` descending, ties broken by ``best_wpm``
   descending, and remaining ties broken by ``first_best_at``
   ascending. (Requirement 5.3)
2. The ``rank`` values form the contiguous sequence ``1, 2, ..., N``
   in display order. (Requirement 5.4)
3. For any adjacent pair ``(a, b)``, one of the ordering branches
   holds strictly (points/wpm) or as a non-strict inequality on
   ``first_best_at`` when both primary keys tie.

This is the classic "sorted" property expressed as a pairwise check,
which is equivalent to full-list sortedness under a total order but
produces more informative counter-examples.

Like the aggregation property, this test opens a fresh in-memory
SQLite engine per ``@given`` example so it stays stateless across
examples.
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
        "leaderboard-ordering",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("leaderboard-ordering")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Pool size chosen to give Hypothesis room to generate ties on the
#: primary / secondary sort keys and to exercise the contiguous-rank
#: invariant across a non-trivial N.
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


def _as_utc(dt: datetime) -> datetime:
    """Re-attach UTC if SQLite stripped ``tzinfo`` on read-back."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Narrow ``points`` and ``wpm`` ranges so ties are likely — the
# property is most interesting when the secondary / tertiary tie-
# breakers actually fire.
_score_tuple_strategy = st.tuples(
    st.integers(min_value=0, max_value=_PLAYER_POOL_SIZE - 1),  # player_idx
    st.floats(
        min_value=0.0,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # wpm — constrained range bumps up tie probability
    st.floats(
        min_value=0.0,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),  # accuracy — not a sort key, included for realism
    st.integers(min_value=0, max_value=50),  # points — narrow => frequent ties
    st.integers(min_value=0, max_value=86_400),  # created_at offset seconds
)

_score_list_strategy = st.lists(
    _score_tuple_strategy,
    min_size=1,
    max_size=30,
)


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


@given(score_specs=_score_list_strategy)
@settings(max_examples=30, deadline=None)
def test_leaderboard_ordering_and_rank_invariant(
    score_specs: list[tuple[int, float, float, int, int]],
) -> None:
    """Property 10: entries are in the required order with contiguous ranks.

    Validates Requirements 5.3, 5.4.
    """
    engine = _build_engine()
    session_factory = _build_session_factory(engine)
    player_ids, prompt_id = _seed_players_and_prompt(session_factory)

    with session_factory() as s:
        for player_idx, wpm, accuracy, points, offset_seconds in score_specs:
            created_at = _BASE_TIME + timedelta(seconds=offset_seconds)
            _insert_score(
                s,
                player_id=player_ids[player_idx],
                prompt_id=prompt_id,
                points=points,
                wpm=wpm,
                accuracy=accuracy,
                created_at=created_at,
            )
        s.commit()

    entries = LeaderboardService(session_factory).build_snapshot().entries

    # --- Requirement 5.4: contiguous ranks ``1, 2, ..., N``. ---
    expected_ranks = list(range(1, len(entries) + 1))
    actual_ranks = [e.rank for e in entries]
    assert actual_ranks == expected_ranks, (
        f"ranks must be contiguous 1..N; got {actual_ranks}"
    )

    # --- Requirement 5.3: pairwise ordering check. ---
    # For each adjacent pair (a, b), exactly one of:
    #   1. a.best_points >  b.best_points
    #   2. a.best_points == b.best_points AND a.best_wpm >  b.best_wpm
    #   3. a.best_points == b.best_points AND a.best_wpm == b.best_wpm
    #      AND a.first_best_at <= b.first_best_at
    # must hold. The first matching branch wins; we express this as
    # the full composite-key comparison ``key(a) <= key(b)``, which
    # is equivalent and yields a cleaner error message.
    for a, b in zip(entries, entries[1:]):
        a_first = _as_utc(a.first_best_at)
        b_first = _as_utc(b.first_best_at)

        key_a = (-a.best_points, -a.best_wpm, a_first)
        key_b = (-b.best_points, -b.best_wpm, b_first)
        assert key_a <= key_b, (
            "adjacent leaderboard entries violate ordering: "
            f"a=(points={a.best_points}, wpm={a.best_wpm}, "
            f"first_best_at={a_first!r}) should come before "
            f"b=(points={b.best_points}, wpm={b.best_wpm}, "
            f"first_best_at={b_first!r})"
        )

        # Also assert the tie-break disjunction explicitly — if this
        # fails with the composite-key check above passing, something
        # is very wrong with Python's tuple comparison semantics.
        strict_points = a.best_points > b.best_points
        tie_points_strict_wpm = (
            a.best_points == b.best_points and a.best_wpm > b.best_wpm
        )
        tie_both_ts_ok = (
            a.best_points == b.best_points
            and a.best_wpm == b.best_wpm
            and a_first <= b_first
        )
        assert strict_points or tie_points_strict_wpm or tie_both_ts_ok, (
            "no ordering branch holds for adjacent entries "
            f"a={a!r}, b={b!r}"
        )
