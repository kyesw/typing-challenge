"""Deterministic unit tests for ``ScoringService.compute_and_persist`` (task 5.2).

Covers:

1. Happy path: Score row inserted with fields derived from
   server-measured elapsed; Game transitioned to COMPLETED with
   ``ended_at`` stamped.
2. ``ScoreAlreadyExists`` when a Score already exists for the Game.
3. ``GameNotEligible`` when the Game isn't in ``in_progress``.
4. ``GameNotEligible`` when ``started_at`` is NULL.
5. ``GameNotEligible`` when ``ended_at <= started_at`` (Requirement
   8.7 invariant).
6. Sanity: scoring uses server elapsed; the ``typed_text`` is
   compared against the prompt text per the pure scoring helpers.

The session is created by the test fixture and passed into the
service so both sides of the transaction are visible to the
assertions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.scoring import compute_accuracy, compute_points, compute_wpm
from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    Score,
    init_db,
)
from app.services import (
    GameNotEligible,
    RecordScoreSuccess,
    ScoreAlreadyExists,
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


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _seed_player_prompt_game(
    session_factory: sessionmaker[Session],
    *,
    game_status: GameStatus = GameStatus.IN_PROGRESS,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    prompt_text: str = PROMPT_TEXT,
    nickname: str | None = None,
) -> tuple[str, str, str]:
    """Insert a player, a prompt, and a Game. Return their ids."""
    player_id = str(uuid.uuid4())
    prompt_id = str(uuid.uuid4())
    game_id = str(uuid.uuid4())
    # Default to a nickname unique per call — multiple seeded players
    # in one test would otherwise collide on ``players.nickname_ci``.
    nick = nickname if nickname is not None else f"p-{player_id[:8]}"
    with session_factory() as s:
        s.add(
            Player(
                id=player_id,
                nickname=nick,
                nickname_ci=nick.lower(),
                session_token=str(uuid.uuid4()),
                session_expires_at=_future(),
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
                status=game_status,
                started_at=started_at,
                ended_at=ended_at,
            )
        )
        s.commit()
    return player_id, prompt_id, game_id


def _id_stream(ids):
    it = iter(ids)

    def factory() -> str:
        return next(it)

    return factory


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_compute_and_persist_writes_score_and_transitions_game(
    session_factory: sessionmaker[Session],
) -> None:
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=30)
    player_id, prompt_id, game_id = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    pinned_score_id = str(uuid.uuid4())
    service = ScoringService(id_factory=_id_stream([pinned_score_id]))

    typed = PROMPT_TEXT  # perfect typing
    with session_factory() as session:
        game = session.get(Game, game_id)
        assert game is not None
        result = service.compute_and_persist(session, game, typed, ended_at)
        # The caller commits; we do here to persist the transition.
        session.commit()

    assert isinstance(result, RecordScoreSuccess)
    assert result.game_id == game_id
    assert result.player_id == player_id
    assert result.score_id == pinned_score_id
    assert result.status is GameStatus.COMPLETED
    assert result.ended_at == ended_at
    assert result.elapsed_seconds == 30.0

    # Score should match the pure helpers with the server elapsed.
    expected_wpm = compute_wpm(typed, PROMPT_TEXT, 30.0)
    expected_accuracy = compute_accuracy(typed, PROMPT_TEXT)
    expected_points = compute_points(expected_wpm, expected_accuracy)
    assert result.wpm == expected_wpm
    assert result.accuracy == expected_accuracy
    assert result.points == expected_points

    # DB state: Score persisted, Game transitioned.
    with session_factory() as s:
        score_row = s.execute(
            select(Score).where(Score.game_id == game_id)
        ).scalar_one()
        assert score_row.id == pinned_score_id
        assert score_row.player_id == player_id
        assert score_row.wpm == expected_wpm
        assert score_row.accuracy == expected_accuracy
        assert score_row.points == expected_points

        game_row = s.get(Game, game_id)
        assert game_row is not None
        assert game_row.status is GameStatus.COMPLETED
        assert game_row.ended_at is not None
        ended_at_observed = game_row.ended_at
        if ended_at_observed.tzinfo is None:
            ended_at_observed = ended_at_observed.replace(tzinfo=timezone.utc)
        assert ended_at_observed == ended_at


# ---------------------------------------------------------------------------
# 2. Already scored — ScoreAlreadyExists
# ---------------------------------------------------------------------------


def test_compute_and_persist_returns_already_exists_when_score_present(
    session_factory: sessionmaker[Session],
) -> None:
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(seconds=30)
    player_id, _, game_id = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    existing_score_id = str(uuid.uuid4())
    with session_factory() as s:
        s.add(
            Score(
                id=existing_score_id,
                game_id=game_id,
                player_id=player_id,
                wpm=42.0,
                accuracy=100.0,
                points=100,
            )
        )
        s.commit()

    service = ScoringService()

    with session_factory() as session:
        game = session.get(Game, game_id)
        assert game is not None
        result = service.compute_and_persist(session, game, "xxxx", ended_at)
        session.commit()

    assert isinstance(result, ScoreAlreadyExists)
    assert result.game_id == game_id
    assert result.score_id == existing_score_id

    # Exactly one Score still.
    with session_factory() as s:
        rows = s.execute(select(Score).where(Score.game_id == game_id)).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == existing_score_id


# ---------------------------------------------------------------------------
# 3. Wrong status → GameNotEligible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [GameStatus.PENDING, GameStatus.COMPLETED, GameStatus.ABANDONED],
)
def test_compute_and_persist_rejects_non_in_progress_game(
    session_factory: sessionmaker[Session], status: GameStatus
) -> None:
    # Use timestamps compatible with the status so the check constraint
    # is satisfied.
    if status is GameStatus.PENDING:
        started_at, ended_at_seed = None, None
    elif status is GameStatus.COMPLETED:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        ended_at_seed = started_at + timedelta(seconds=10)
    else:  # ABANDONED
        started_at = datetime.now(timezone.utc) - timedelta(seconds=200)
        ended_at_seed = started_at + timedelta(seconds=5)

    _, _, game_id = _seed_player_prompt_game(
        session_factory,
        game_status=status,
        started_at=started_at,
        ended_at=ended_at_seed,
    )

    ended_at = datetime.now(timezone.utc)
    service = ScoringService()

    with session_factory() as session:
        game = session.get(Game, game_id)
        assert game is not None
        result = service.compute_and_persist(session, game, "xxxx", ended_at)
        # No commit: nothing should have been written.

    assert isinstance(result, GameNotEligible)
    assert result.game_id == game_id
    assert result.current_status is status
    assert result.reason == "not_in_progress"

    # No Score row written.
    with session_factory() as s:
        rows = s.execute(select(Score).where(Score.game_id == game_id)).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# 4. started_at is NULL on an IN_PROGRESS row → GameNotEligible
# ---------------------------------------------------------------------------


def test_compute_and_persist_rejects_when_started_at_is_null(
    session_factory: sessionmaker[Session],
) -> None:
    _, _, game_id = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=None,  # corrupted: IN_PROGRESS without started_at
    )

    service = ScoringService()
    ended_at = datetime.now(timezone.utc)

    with session_factory() as session:
        game = session.get(Game, game_id)
        assert game is not None
        result = service.compute_and_persist(session, game, "xxxx", ended_at)

    assert isinstance(result, GameNotEligible)
    assert result.current_status is GameStatus.IN_PROGRESS
    assert result.reason == "missing_started_at"


# ---------------------------------------------------------------------------
# 5. ended_at <= started_at → GameNotEligible
# ---------------------------------------------------------------------------


def test_compute_and_persist_rejects_when_ended_at_not_after_started_at(
    session_factory: sessionmaker[Session],
) -> None:
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _, _, game_id = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = ScoringService()

    with session_factory() as session:
        game = session.get(Game, game_id)
        assert game is not None
        # ended_at == started_at — not strictly greater.
        result = service.compute_and_persist(session, game, "xxxx", started_at)

    assert isinstance(result, GameNotEligible)
    assert result.current_status is GameStatus.IN_PROGRESS
    # The reason is 'missing_started_at' because the row is
    # structurally unscorable on the clocks we were given.
    assert result.reason == "missing_started_at"


# ---------------------------------------------------------------------------
# 6. Server elapsed is authoritative: typed text alone cannot change WPM
# ---------------------------------------------------------------------------


def test_compute_and_persist_uses_server_elapsed_from_game_row(
    session_factory: sessionmaker[Session],
) -> None:
    """Same typed_text + same prompt → WPM is a function of elapsed only.

    Two identical submissions against different ``ended_at`` values
    must produce different WPM values (unless the pure function
    saturates at 0). This is the service-level echo of Property 7
    from the design document.
    """
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    typed = PROMPT_TEXT

    # Two independent games with identical seeds, different ended_at.
    _, _, g1 = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )
    _, _, g2 = _seed_player_prompt_game(
        session_factory,
        game_status=GameStatus.IN_PROGRESS,
        started_at=started_at,
    )

    service = ScoringService()

    with session_factory() as session:
        g1_row = session.get(Game, g1)
        assert g1_row is not None
        r1 = service.compute_and_persist(
            session, g1_row, typed, started_at + timedelta(seconds=30)
        )
        session.commit()

    with session_factory() as session:
        g2_row = session.get(Game, g2)
        assert g2_row is not None
        r2 = service.compute_and_persist(
            session, g2_row, typed, started_at + timedelta(seconds=60)
        )
        session.commit()

    assert isinstance(r1, RecordScoreSuccess)
    assert isinstance(r2, RecordScoreSuccess)

    # Typing the same text in half the time yields double WPM.
    assert r1.wpm == pytest.approx(r2.wpm * 2, rel=1e-9)
