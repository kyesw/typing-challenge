"""Unit tests for the persistence layer (task 2.1).

Covers the DB-level invariants that task 2.1 is responsible for:

- ``create_all`` produces all four tables with the required columns.
- Case-insensitive uniqueness on ``players.nickname_ci`` (Requirement 1.7).
- ``scores.game_id`` UNIQUE constraint (Requirement 4.4 / Property 8).
- ``games.ended_at > started_at`` CHECK constraint (Requirement 8.7 /
  Property 8).
- ``scores.wpm >= 0`` and ``accuracy`` in ``[0, 100]`` CHECK constraints
  (Requirements 4.1, 4.2).
- Leaderboard-query indexes exist (``scores.player_id``,
  ``scores.points``, ``scores.created_at``) and the session-token
  lookup index exists on ``players`` (Requirement 5.7 / design
  "Performance Considerations").
- ``players`` table columns match exactly the Requirement 16.2 allowlist
  (Property 20 smoke).

Property-based tests for nickname validation (task 2.3) and prompt
validity (task 2.5) are intentionally NOT implemented here — those
belong to separate tasks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.persistence import Game, GameStatus, Player, Prompt, Score, init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Fresh in-memory SQLite engine with the schema materialized.

    SQLite does not enforce CHECK constraints or foreign keys by default
    unless the connection opts in; we enable foreign keys per-connection
    so the FK wiring on ``games`` and ``scores`` behaves like it will
    on the real deployment.
    """
    eng = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(eng)
    return eng


@pytest.fixture()
def session(engine: Engine) -> Session:
    with Session(engine) as s:
        yield s


def _future() -> datetime:
    """A plain expires-at value comfortably in the future."""
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _make_player(
    session: Session,
    *,
    nickname: str,
    nickname_ci: str | None = None,
    token: str | None = None,
) -> Player:
    player = Player(
        id=str(uuid.uuid4()),
        nickname=nickname,
        nickname_ci=(nickname_ci if nickname_ci is not None else nickname.lower()),
        session_token=token or str(uuid.uuid4()),
        session_expires_at=_future(),
    )
    session.add(player)
    session.commit()
    return player


def _make_prompt(session: Session) -> Prompt:
    prompt = Prompt(
        id=str(uuid.uuid4()),
        text="x" * 120,  # any non-empty string; length rule lives in validator
        difficulty=None,
        language="en",
    )
    session.add(prompt)
    session.commit()
    return prompt


def _make_game(
    session: Session,
    *,
    player: Player,
    prompt: Prompt,
    status: GameStatus = GameStatus.COMPLETED,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> Game:
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    if ended_at is None and status in (GameStatus.COMPLETED, GameStatus.ABANDONED):
        ended_at = started_at + timedelta(seconds=30)
    game = Game(
        id=str(uuid.uuid4()),
        player_id=player.id,
        prompt_id=prompt.id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
    )
    session.add(game)
    session.commit()
    return game


# ---------------------------------------------------------------------------
# 1. create_all produces all four tables with the required columns
# ---------------------------------------------------------------------------


def test_create_all_produces_all_four_tables(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"players", "prompts", "games", "scores"}.issubset(tables)

    # Spot-check expected columns per table; the Property 20 / Requirement
    # 16.2 allowlist for players gets a stricter check below.
    prompt_cols = {c["name"] for c in insp.get_columns("prompts")}
    assert {"id", "text", "difficulty", "language"}.issubset(prompt_cols)

    game_cols = {c["name"] for c in insp.get_columns("games")}
    assert {
        "id",
        "player_id",
        "prompt_id",
        "status",
        "started_at",
        "ended_at",
    }.issubset(game_cols)

    score_cols = {c["name"] for c in insp.get_columns("scores")}
    assert {
        "id",
        "game_id",
        "player_id",
        "wpm",
        "accuracy",
        "points",
        "created_at",
    }.issubset(score_cols)


# ---------------------------------------------------------------------------
# 2. Case-insensitive uniqueness on players.nickname_ci
# ---------------------------------------------------------------------------


def test_nickname_ci_enforces_case_insensitive_uniqueness(session: Session) -> None:
    _make_player(session, nickname="Alice", nickname_ci="alice")
    # Same case-folded key → must raise IntegrityError on commit.
    duplicate = Player(
        id=str(uuid.uuid4()),
        nickname="alice",
        nickname_ci="alice",
        session_token=str(uuid.uuid4()),
        session_expires_at=_future(),
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()


# ---------------------------------------------------------------------------
# 3. scores.game_id UNIQUE constraint
# ---------------------------------------------------------------------------


def test_scores_game_id_is_unique(session: Session) -> None:
    player = _make_player(session, nickname="Bob", nickname_ci="bob")
    prompt = _make_prompt(session)
    game = _make_game(session, player=player, prompt=prompt)

    session.add(
        Score(
            id=str(uuid.uuid4()),
            game_id=game.id,
            player_id=player.id,
            wpm=40.0,
            accuracy=95.0,
            points=380,
        )
    )
    session.commit()

    session.add(
        Score(
            id=str(uuid.uuid4()),
            game_id=game.id,  # duplicate gameId — must fail
            player_id=player.id,
            wpm=42.0,
            accuracy=96.0,
            points=400,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# ---------------------------------------------------------------------------
# 4. games ended_at > started_at CHECK constraint
# ---------------------------------------------------------------------------


def test_games_end_must_be_after_start(session: Session) -> None:
    player = _make_player(session, nickname="Carol", nickname_ci="carol")
    prompt = _make_prompt(session)

    now = datetime.now(timezone.utc)
    bad = Game(
        id=str(uuid.uuid4()),
        player_id=player.id,
        prompt_id=prompt.id,
        status=GameStatus.COMPLETED,
        started_at=now,
        ended_at=now,  # equal — violates strict >
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_games_allow_null_started_or_ended(session: Session) -> None:
    """The CHECK tolerates either column being NULL (pending / in_progress)."""
    player = _make_player(session, nickname="Dave", nickname_ci="dave")
    prompt = _make_prompt(session)

    # pending: no timestamps at all
    pending = _make_game(
        session,
        player=player,
        prompt=prompt,
        status=GameStatus.PENDING,
        started_at=None,
        ended_at=None,
    )
    assert pending.status is GameStatus.PENDING

    # in_progress: started_at set, ended_at NULL
    in_progress = _make_game(
        session,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
    )
    assert in_progress.status is GameStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 5. scores CHECK constraints on wpm / accuracy
# ---------------------------------------------------------------------------


def test_scores_wpm_must_be_non_negative(session: Session) -> None:
    player = _make_player(session, nickname="Eve", nickname_ci="eve")
    prompt = _make_prompt(session)
    game = _make_game(session, player=player, prompt=prompt)

    session.add(
        Score(
            id=str(uuid.uuid4()),
            game_id=game.id,
            player_id=player.id,
            wpm=-0.1,  # out of range
            accuracy=80.0,
            points=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("bad_accuracy", [-1.0, 100.5, 250.0])
def test_scores_accuracy_must_be_within_0_100(
    session: Session, bad_accuracy: float
) -> None:
    player = _make_player(session, nickname=f"Fay{bad_accuracy}", nickname_ci=f"fay{bad_accuracy}")
    prompt = _make_prompt(session)
    game = _make_game(session, player=player, prompt=prompt)

    session.add(
        Score(
            id=str(uuid.uuid4()),
            game_id=game.id,
            player_id=player.id,
            wpm=30.0,
            accuracy=bad_accuracy,
            points=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# ---------------------------------------------------------------------------
# 6. Required indexes exist
# ---------------------------------------------------------------------------


def test_leaderboard_indexes_exist_on_scores(engine: Engine) -> None:
    insp = inspect(engine)
    score_indexes = insp.get_indexes("scores")
    indexed_columns = {
        tuple(ix["column_names"]) for ix in score_indexes if ix.get("column_names")
    }
    # Each of the three leaderboard-query columns has a single-column index.
    assert ("player_id",) in indexed_columns
    assert ("points",) in indexed_columns
    assert ("created_at",) in indexed_columns


def test_session_token_index_exists_on_players(engine: Engine) -> None:
    insp = inspect(engine)
    indexed_columns = {
        tuple(ix["column_names"])
        for ix in insp.get_indexes("players")
        if ix.get("column_names")
    }
    assert ("session_token",) in indexed_columns


# ---------------------------------------------------------------------------
# 7. players table columns match the Requirement 16.2 allowlist
# ---------------------------------------------------------------------------


def test_players_columns_match_privacy_allowlist(engine: Engine) -> None:
    """Property 20 / Requirement 16.2: no additional personal-data fields."""
    insp = inspect(engine)
    columns = {c["name"] for c in insp.get_columns("players")}
    assert columns == {
        "id",
        "nickname",
        "nickname_ci",
        "created_at",
        "session_token",
        "session_expires_at",
    }


# ---------------------------------------------------------------------------
# Sanity: the in-memory DB really speaks SQL (smoke over the raw connection)
# ---------------------------------------------------------------------------


def test_engine_is_sqlite_and_responds(engine: Engine) -> None:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar_one()
    assert result == 1
