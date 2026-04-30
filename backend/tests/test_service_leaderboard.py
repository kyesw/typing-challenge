"""Unit tests for ``LeaderboardService.build_snapshot`` (task 6.1).

Covers Requirements 5.1–5.5:

- 5.1 — one entry per Player with ≥1 Score.
- 5.2 — per-player max of points / wpm / accuracy.
- 5.3 — order: ``bestPoints desc, bestWpm desc, earliest createdAt asc``.
- 5.4 — contiguous ranks starting at 1.
- 5.5 — read-only (no writes); exercised implicitly by asserting
       the ``scores`` / ``players`` row counts don't change across
       the call.

Also covers the service-level ``limit`` parameter used by the
dashboard's top-N view.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
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


_BASE_TIME = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _as_utc(dt: datetime) -> datetime:
    """Re-attach UTC ``tzinfo`` if SQLite stripped it on read-back.

    SQLite's default datetime handling returns naive datetimes for
    ``DateTime(timezone=True)`` columns; the rest of this repo
    handles that by round-tripping the tzinfo before comparing.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _make_player(session: Session, nickname: str) -> str:
    """Insert a Player with a unique id. Returns the player id."""
    player_id = str(uuid.uuid4())
    session.add(
        Player(
            id=player_id,
            nickname=nickname,
            nickname_ci=nickname.lower(),
            session_token=str(uuid.uuid4()),
            session_expires_at=_future(),
        )
    )
    return player_id


def _make_prompt(session: Session) -> str:
    """Insert a throwaway prompt and return its id."""
    prompt_id = str(uuid.uuid4())
    session.add(
        Prompt(
            id=prompt_id,
            # Length must fall within the validator's 100–500 bounds
            # for the seed path, but the DB itself doesn't enforce
            # that; we still pick a safe value for defense in depth.
            text="a" * 120,
            difficulty=None,
            language="en",
        )
    )
    return prompt_id


def _make_game(
    session: Session,
    player_id: str,
    prompt_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
) -> str:
    game_id = str(uuid.uuid4())
    session.add(
        Game(
            id=game_id,
            player_id=player_id,
            prompt_id=prompt_id,
            status=GameStatus.COMPLETED,
            started_at=started_at,
            ended_at=ended_at,
        )
    )
    return game_id


def _make_score(
    session: Session,
    *,
    player_id: str,
    game_id: str,
    points: int,
    wpm: float,
    accuracy: float,
    created_at: datetime,
) -> str:
    score_id = str(uuid.uuid4())
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
    return score_id


def _seed_scores(
    session_factory: sessionmaker[Session],
    specs: list[dict],
) -> dict[str, str]:
    """Seed players + scores from a list of dict specs.

    Each spec is ``{"nickname": str, "points": int, "wpm": float,
    "accuracy": float, "created_at": datetime}``. Multiple specs with
    the same nickname create multiple Scores for that player (one Game
    per Score so the ``scores.game_id`` UNIQUE constraint holds).

    Returns a ``{nickname: player_id}`` map so tests can reference
    players by their stable display name.
    """
    nickname_to_id: dict[str, str] = {}
    with session_factory() as session:
        prompt_id = _make_prompt(session)
        for spec in specs:
            nick = spec["nickname"]
            if nick not in nickname_to_id:
                nickname_to_id[nick] = _make_player(session, nick)
            player_id = nickname_to_id[nick]

            created_at: datetime = spec["created_at"]
            # started_at < ended_at < created_at keeps the CHECK
            # ``ended_at > started_at`` satisfied; the leaderboard
            # query only looks at the Score row, so exact values
            # don't matter here.
            game_id = _make_game(
                session,
                player_id,
                prompt_id,
                started_at=created_at - timedelta(seconds=2),
                ended_at=created_at - timedelta(seconds=1),
            )
            _make_score(
                session,
                player_id=player_id,
                game_id=game_id,
                points=spec["points"],
                wpm=spec["wpm"],
                accuracy=spec["accuracy"],
                created_at=created_at,
            )
        session.commit()
    return nickname_to_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_store_returns_no_entries(
    session_factory: sessionmaker[Session],
) -> None:
    """With no scores persisted, the snapshot has an empty entry list.

    Requirement 5.1 implicitly: zero players with scores → zero entries.
    """
    fixed_now = _BASE_TIME + timedelta(days=1)
    service = LeaderboardService(session_factory, clock=lambda: fixed_now)

    snapshot = service.build_snapshot()

    assert snapshot.entries == []
    # ``generated_at`` comes from the injected clock, not wall time.
    assert snapshot.generated_at == fixed_now


def test_single_player_single_score(
    session_factory: sessionmaker[Session],
) -> None:
    """One player with one score → one ranked entry at rank 1."""
    created_at = _BASE_TIME
    _seed_scores(
        session_factory,
        [
            {
                "nickname": "alice",
                "points": 420,
                "wpm": 42.0,
                "accuracy": 95.0,
                "created_at": created_at,
            },
        ],
    )

    service = LeaderboardService(session_factory)
    snapshot = service.build_snapshot()

    assert len(snapshot.entries) == 1
    entry = snapshot.entries[0]
    assert entry.nickname == "alice"
    assert entry.best_points == 420
    assert entry.best_wpm == 42.0
    assert entry.best_accuracy == 95.0
    assert entry.rank == 1
    assert _as_utc(entry.first_best_at) == created_at


def test_orders_by_best_points_descending(
    session_factory: sessionmaker[Session],
) -> None:
    """Primary sort key is ``best_points`` descending (Req 5.3)."""
    _seed_scores(
        session_factory,
        [
            {
                "nickname": "low",
                "points": 100,
                "wpm": 30.0,
                "accuracy": 80.0,
                "created_at": _BASE_TIME,
            },
            {
                "nickname": "high",
                "points": 900,
                "wpm": 90.0,
                "accuracy": 99.0,
                "created_at": _BASE_TIME + timedelta(seconds=5),
            },
            {
                "nickname": "mid",
                "points": 500,
                "wpm": 50.0,
                "accuracy": 90.0,
                "created_at": _BASE_TIME + timedelta(seconds=10),
            },
        ],
    )

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    assert [e.nickname for e in entries] == ["high", "mid", "low"]
    assert [e.rank for e in entries] == [1, 2, 3]


def test_tie_on_points_breaks_on_wpm(
    session_factory: sessionmaker[Session],
) -> None:
    """Equal ``best_points`` → the higher ``best_wpm`` ranks first."""
    _seed_scores(
        session_factory,
        [
            {
                "nickname": "slow",
                "points": 500,
                "wpm": 40.0,
                "accuracy": 95.0,
                "created_at": _BASE_TIME,
            },
            {
                "nickname": "fast",
                "points": 500,
                "wpm": 70.0,
                "accuracy": 80.0,
                "created_at": _BASE_TIME + timedelta(seconds=1),
            },
        ],
    )

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    assert [e.nickname for e in entries] == ["fast", "slow"]
    assert [e.rank for e in entries] == [1, 2]


def test_tie_on_points_and_wpm_breaks_on_first_best_at(
    session_factory: sessionmaker[Session],
) -> None:
    """Equal ``points`` and ``wpm`` → earlier ``first_best_at`` wins."""
    early = _BASE_TIME
    late = _BASE_TIME + timedelta(minutes=5)
    _seed_scores(
        session_factory,
        [
            {
                "nickname": "latecomer",
                "points": 500,
                "wpm": 55.0,
                "accuracy": 85.0,
                "created_at": late,
            },
            {
                "nickname": "early_bird",
                "points": 500,
                "wpm": 55.0,
                "accuracy": 90.0,
                "created_at": early,
            },
        ],
    )

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    assert [e.nickname for e in entries] == ["early_bird", "latecomer"]
    assert _as_utc(entries[0].first_best_at) == early
    assert _as_utc(entries[1].first_best_at) == late


def test_per_player_aggregation_uses_maxima(
    session_factory: sessionmaker[Session],
) -> None:
    """Multiple scores per player → the entry uses per-field maxima.

    Requirement 5.2 / Property 9: best_points, best_wpm, best_accuracy
    are independent maxima. A player's best_wpm need not come from
    the same Score as their best_points.
    """
    _seed_scores(
        session_factory,
        [
            # Highest points, moderate wpm, moderate accuracy.
            {
                "nickname": "veteran",
                "points": 700,
                "wpm": 55.0,
                "accuracy": 88.0,
                "created_at": _BASE_TIME,
            },
            # Highest wpm, lower points.
            {
                "nickname": "veteran",
                "points": 600,
                "wpm": 80.0,
                "accuracy": 70.0,
                "created_at": _BASE_TIME + timedelta(minutes=1),
            },
            # Highest accuracy, lower points and wpm.
            {
                "nickname": "veteran",
                "points": 400,
                "wpm": 45.0,
                "accuracy": 99.5,
                "created_at": _BASE_TIME + timedelta(minutes=2),
            },
        ],
    )

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    assert len(entries) == 1
    entry = entries[0]
    assert entry.best_points == 700
    assert entry.best_wpm == 80.0
    assert entry.best_accuracy == 99.5
    assert entry.rank == 1


def test_first_best_at_is_earliest_score_matching_best_points(
    session_factory: sessionmaker[Session],
) -> None:
    """``first_best_at`` = earliest ``created_at`` among scores tied on best_points.

    Two players tied on ``best_points`` with multiple scores each:
    the tie-breaking ``first_best_at`` must be the earliest
    best-matching timestamp, not the earliest timestamp overall.
    """
    # "solo" hits 500 only once, early.
    solo_hit = _BASE_TIME + timedelta(seconds=10)
    # "repeat" has a low early score then hits 500 twice; the
    # tie-break must use the first 500, not the first score.
    repeat_low = _BASE_TIME  # earliest overall, but only 300 points
    repeat_first_best = _BASE_TIME + timedelta(seconds=20)  # first 500
    repeat_second_best = _BASE_TIME + timedelta(seconds=30)  # second 500

    _seed_scores(
        session_factory,
        [
            {
                "nickname": "repeat",
                "points": 300,
                "wpm": 40.0,
                "accuracy": 80.0,
                "created_at": repeat_low,
            },
            {
                "nickname": "solo",
                "points": 500,
                "wpm": 60.0,
                "accuracy": 90.0,
                "created_at": solo_hit,
            },
            {
                "nickname": "repeat",
                "points": 500,
                "wpm": 60.0,
                "accuracy": 91.0,
                "created_at": repeat_first_best,
            },
            {
                "nickname": "repeat",
                "points": 500,
                "wpm": 60.0,
                "accuracy": 92.0,
                "created_at": repeat_second_best,
            },
        ],
    )

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    by_nick = {e.nickname: e for e in entries}

    # ``repeat``'s first_best_at is the first time they hit 500, not
    # the timestamp of their earlier 300-point score.
    assert _as_utc(by_nick["repeat"].first_best_at) == repeat_first_best
    assert _as_utc(by_nick["solo"].first_best_at) == solo_hit

    # ``solo`` hit 500 at t+10s; ``repeat`` hit 500 at t+20s. So
    # ``solo`` ranks first on the created_at tie-break.
    assert [e.nickname for e in entries] == ["solo", "repeat"]
    assert [e.rank for e in entries] == [1, 2]


def test_ranks_are_contiguous_starting_at_one(
    session_factory: sessionmaker[Session],
) -> None:
    """Ranks form the contiguous sequence ``1, 2, ..., N`` (Req 5.4)."""
    specs = []
    # 7 players with distinct descending points.
    for i in range(7):
        specs.append(
            {
                "nickname": f"player_{i}",
                "points": 1000 - i * 10,
                "wpm": 50.0,
                "accuracy": 90.0,
                "created_at": _BASE_TIME + timedelta(seconds=i),
            }
        )

    _seed_scores(session_factory, specs)

    service = LeaderboardService(session_factory)
    entries = service.build_snapshot().entries

    assert [e.rank for e in entries] == [1, 2, 3, 4, 5, 6, 7]
    # Ordering aligns with descending points (player_0 is best).
    assert [e.nickname for e in entries] == [
        f"player_{i}" for i in range(7)
    ]


def test_limit_truncates_entries(
    session_factory: sessionmaker[Session],
) -> None:
    """``limit`` truncates to top-N while ranks remain absolute."""
    specs = [
        {
            "nickname": f"p{i}",
            "points": 1000 - i * 10,
            "wpm": 50.0,
            "accuracy": 90.0,
            "created_at": _BASE_TIME + timedelta(seconds=i),
        }
        for i in range(5)
    ]
    _seed_scores(session_factory, specs)

    service = LeaderboardService(session_factory)

    full = service.build_snapshot().entries
    assert len(full) == 5

    top3 = service.build_snapshot(limit=3).entries
    assert len(top3) == 3
    assert [e.rank for e in top3] == [1, 2, 3]
    # The three returned entries are the three best — same as the
    # first three of the full list.
    assert [e.nickname for e in top3] == [e.nickname for e in full[:3]]

    # Limit larger than N returns everything.
    top10 = service.build_snapshot(limit=10).entries
    assert [e.nickname for e in top10] == [e.nickname for e in full]

    # Limit of 0 returns empty.
    top0 = service.build_snapshot(limit=0).entries
    assert top0 == []


def test_build_snapshot_does_not_mutate_store(
    session_factory: sessionmaker[Session],
) -> None:
    """Read-only derivation — Requirement 5.5.

    Round-trip the row counts across a ``build_snapshot`` call and
    assert they are unchanged. A regression that caused the service
    to (e.g.) materialize LeaderboardEntry rows into the DB would
    show up here as a delta.
    """
    _seed_scores(
        session_factory,
        [
            {
                "nickname": "alice",
                "points": 300,
                "wpm": 40.0,
                "accuracy": 85.0,
                "created_at": _BASE_TIME,
            },
            {
                "nickname": "bob",
                "points": 400,
                "wpm": 50.0,
                "accuracy": 90.0,
                "created_at": _BASE_TIME + timedelta(seconds=5),
            },
        ],
    )

    def _row_counts() -> tuple[int, int, int, int]:
        with session_factory() as s:
            return (
                s.execute(select(Player)).all().__len__(),
                s.execute(select(Prompt)).all().__len__(),
                s.execute(select(Game)).all().__len__(),
                s.execute(select(Score)).all().__len__(),
            )

    before = _row_counts()
    LeaderboardService(session_factory).build_snapshot()
    after = _row_counts()

    assert before == after
