"""Deterministic unit tests for ``GameService.create_game`` (task 4.2).

These tests exercise the five cases called out by the task brief:

1. Player-not-found: creation of a Game for an unknown ``player_id``
   returns :class:`PlayerNotFound` without writing anything.
2. Happy path: creation of a Game writes a PENDING row referencing the
   seeded prompt and returns a well-formed success payload.
3. Conflict on in_progress: an existing Game in status ``in_progress``
   blocks a new creation and the conflict payload echoes the existing
   ``game_id``.
4. Conflict on pending: an existing Game in status ``pending`` blocks
   creation identically to the in_progress case, so the service cannot
   leak a second live Game for the same player.
5. Terminal statuses do NOT block: ``completed`` and ``abandoned``
   Games for a player leave the player free to start a new one.

The ``id_factory``, ``clock``, and the Prompt_Repository's random
source are all pinned so assertions are fully deterministic; the
property-based coverage (tasks 4.7 / 4.8) lives in its own files.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.persistence import (
    Game,
    GameStatus,
    Player,
    Prompt,
    PromptRepository,
    init_db,
)
from app.persistence.prompt_repository import NoPromptsAvailable, SelectedPrompt
from app.services import (
    CreateGameSuccess,
    GameAlreadyInProgress,
    GameService,
    PlayerNotFound,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced."""
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
# Test helpers
# ---------------------------------------------------------------------------


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _make_player(session_factory: sessionmaker[Session], *, nickname: str) -> Player:
    """Insert a Player row and return a detached snapshot."""
    with session_factory() as s:
        player = Player(
            id=str(uuid.uuid4()),
            nickname=nickname,
            nickname_ci=nickname.lower(),
            session_token=str(uuid.uuid4()),
            session_expires_at=_future(),
        )
        s.add(player)
        s.commit()
        # Detach a plain copy so the caller can read fields after the
        # session has closed.
        return Player(
            id=player.id,
            nickname=player.nickname,
            nickname_ci=player.nickname_ci,
            session_token=player.session_token,
            session_expires_at=player.session_expires_at,
        )


def _make_prompt(session_factory: sessionmaker[Session]) -> Prompt:
    """Insert a single prompt row valid per the seed rules."""
    with session_factory() as s:
        prompt = Prompt(
            id=str(uuid.uuid4()),
            text="x" * 120,
            difficulty=None,
            language="en",
        )
        s.add(prompt)
        s.commit()
        return Prompt(
            id=prompt.id,
            text=prompt.text,
            difficulty=prompt.difficulty,
            language=prompt.language,
        )


def _make_game_row(
    session_factory: sessionmaker[Session],
    *,
    player: Player,
    prompt: Prompt,
    status: GameStatus,
) -> str:
    """Insert a Game row with the given status and return its id.

    Supplies the ``started_at`` / ``ended_at`` timestamps only for the
    states that require them, so the CHECK constraint
    ``ended_at > started_at`` stays happy.
    """
    now = datetime.now(timezone.utc)
    started_at: datetime | None
    ended_at: datetime | None
    if status is GameStatus.PENDING:
        started_at, ended_at = None, None
    elif status is GameStatus.IN_PROGRESS:
        started_at, ended_at = now - timedelta(seconds=5), None
    elif status is GameStatus.COMPLETED:
        started_at = now - timedelta(seconds=30)
        ended_at = now - timedelta(seconds=1)
    else:  # ABANDONED
        started_at = now - timedelta(seconds=60)
        ended_at = now - timedelta(seconds=5)

    with session_factory() as s:
        row = Game(
            id=str(uuid.uuid4()),
            player_id=player.id,
            prompt_id=prompt.id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
        )
        s.add(row)
        s.commit()
        return row.id


def _fixed_clock(initial: datetime) -> Callable[[], datetime]:
    state = {"now": initial}

    def clock() -> datetime:
        return state["now"]

    return clock


def _id_stream(ids: Sequence[str]) -> Callable[[], str]:
    it = iter(ids)

    def factory() -> str:
        return next(it)

    return factory


class _FirstIdRandomChoice:
    """Deterministic random-choice stand-in: always picks the first element.

    Good enough for these tests because we only ever seed one prompt
    per case; the point is to avoid depending on ``random.choice``'s
    global state.
    """

    def __call__(self, seq: Sequence[str]) -> str:
        return seq[0]


# ---------------------------------------------------------------------------
# 1. Player not found
# ---------------------------------------------------------------------------


def test_create_game_returns_player_not_found_for_unknown_player(
    session_factory: sessionmaker[Session],
) -> None:
    _make_prompt(session_factory)  # prompt table non-empty so NoPrompts isn't in play
    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(session_factory, repo)

    unknown_id = str(uuid.uuid4())
    result = service.create_game(unknown_id)

    assert isinstance(result, PlayerNotFound)
    assert result.player_id == unknown_id

    # Nothing inserted.
    with session_factory() as s:
        games = s.execute(select(Game)).scalars().all()
    assert games == []


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------


def test_create_game_happy_path_persists_pending_row_and_returns_payload(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Alice")
    prompt = _make_prompt(session_factory)

    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    pinned_game_id = str(uuid.uuid4())
    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(
        session_factory,
        repo,
        clock=_fixed_clock(t0),
        id_factory=_id_stream([pinned_game_id]),
    )

    result = service.create_game(player.id)

    assert isinstance(result, CreateGameSuccess)
    assert result.game_id == pinned_game_id
    assert result.prompt_id == prompt.id
    assert result.prompt_text == prompt.text
    assert result.language == prompt.language
    assert result.status is GameStatus.PENDING
    assert result.started_at == t0

    with session_factory() as s:
        rows = s.execute(select(Game)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == pinned_game_id
    assert row.player_id == player.id
    assert row.prompt_id == prompt.id
    assert row.status is GameStatus.PENDING
    # The persisted row's started_at remains NULL — the returned
    # ``started_at`` is the server's reserved reference clock, not the
    # typing-phase start timestamp (which task 4.3 records).
    assert row.started_at is None
    assert row.ended_at is None


def test_create_game_returns_prompt_chosen_by_repository(
    session_factory: sessionmaker[Session],
) -> None:
    """The service must delegate prompt selection to the repository.

    Wiring a fake repository that returns a known :class:`SelectedPrompt`
    proves the service echoes those fields into the success payload
    rather than inventing them from somewhere else.
    """
    player = _make_player(session_factory, nickname="Bob")
    # Insert a real prompt so the FK on games.prompt_id resolves, but
    # hand a different id to the service via the fake repository and
    # assert the service carries that id through. This doubles as a
    # sanity check that the service trusts the repository's decision.
    real_prompt = _make_prompt(session_factory)

    class _FakeRepo:
        def select_prompt(self) -> SelectedPrompt:
            return SelectedPrompt(
                id=real_prompt.id,
                text="hand-picked prompt text",
                language="xx",
                difficulty="medium",
            )

    service = GameService(
        session_factory,
        _FakeRepo(),  # type: ignore[arg-type]
    )
    result = service.create_game(player.id)

    assert isinstance(result, CreateGameSuccess)
    assert result.prompt_id == real_prompt.id
    assert result.prompt_text == "hand-picked prompt text"
    assert result.language == "xx"


# ---------------------------------------------------------------------------
# 3. Conflict on in_progress
# ---------------------------------------------------------------------------


def test_create_game_conflicts_when_player_has_in_progress_game(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Carol")
    prompt = _make_prompt(session_factory)
    existing_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.IN_PROGRESS,
    )

    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(session_factory, repo)
    result = service.create_game(player.id)

    assert isinstance(result, GameAlreadyInProgress)
    assert result.game_id == existing_id
    assert result.status is GameStatus.IN_PROGRESS

    # No new Game created.
    with session_factory() as s:
        count = s.execute(select(Game)).scalars().all()
    assert len(count) == 1
    assert count[0].id == existing_id


# ---------------------------------------------------------------------------
# 4. Conflict on pending
# ---------------------------------------------------------------------------


def test_create_game_conflicts_when_player_has_pending_game(
    session_factory: sessionmaker[Session],
) -> None:
    """A ``pending`` Game is as "live" as an ``in_progress`` one.

    The per-player single-live-game invariant (Requirement 8.6 /
    design Error Scenario 3) must cover both non-terminal states.
    Without this, the service could produce two ``pending`` rows for
    the same player that no downstream path would resolve.
    """
    player = _make_player(session_factory, nickname="Dave")
    prompt = _make_prompt(session_factory)
    existing_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=GameStatus.PENDING,
    )

    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(session_factory, repo)
    result = service.create_game(player.id)

    assert isinstance(result, GameAlreadyInProgress)
    assert result.game_id == existing_id
    assert result.status is GameStatus.PENDING

    with session_factory() as s:
        rows = s.execute(select(Game)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == existing_id


# ---------------------------------------------------------------------------
# 5. Terminal statuses do not block a new game
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "existing_status", [GameStatus.COMPLETED, GameStatus.ABANDONED]
)
def test_create_game_allows_new_game_when_player_only_has_terminal_games(
    session_factory: sessionmaker[Session],
    existing_status: GameStatus,
) -> None:
    player = _make_player(session_factory, nickname=f"Eve-{existing_status.value}")
    prompt = _make_prompt(session_factory)
    previous_id = _make_game_row(
        session_factory,
        player=player,
        prompt=prompt,
        status=existing_status,
    )

    new_game_id = str(uuid.uuid4())
    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(
        session_factory,
        repo,
        id_factory=_id_stream([new_game_id]),
    )

    result = service.create_game(player.id)

    assert isinstance(result, CreateGameSuccess)
    assert result.game_id == new_game_id
    assert result.game_id != previous_id
    assert result.status is GameStatus.PENDING

    # Exactly two rows now: the terminal one and the fresh pending one.
    with session_factory() as s:
        rows = s.execute(select(Game)).scalars().all()
    ids_by_status = {r.status: r.id for r in rows}
    assert ids_by_status[existing_status] == previous_id
    assert ids_by_status[GameStatus.PENDING] == new_game_id


# ---------------------------------------------------------------------------
# Bonus: empty prompt table surfaces cleanly (guards the seed-data
# deployment invariant — see PromptRepository.NoPromptsAvailable docstring).
# ---------------------------------------------------------------------------


def test_create_game_raises_when_prompt_table_is_empty(
    session_factory: sessionmaker[Session],
) -> None:
    player = _make_player(session_factory, nickname="Frank")
    repo = PromptRepository(session_factory, random_choice=_FirstIdRandomChoice())
    service = GameService(session_factory, repo)

    with pytest.raises(NoPromptsAvailable):
        service.create_game(player.id)
