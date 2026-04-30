"""Deterministic unit tests for ``PlayerService.register`` (task 3.1).

These tests pin the clock and the token_factory so every assertion is
reproducible without Hypothesis — the property-based registration test
is task 3.3 and lives in its own file.

Covers:

1. Happy path: column values and returned payload.
2. Length rejection (too short and too long) — no row is written.
3. Charset rejection — the offending chars are reported; no row is written.
4. Duplicate Active_Player: case-insensitive collision is blocked.
5. Duplicate after expiry: expired row is purged and the new registration
   succeeds, leaving exactly one Active_Player for that ``nickname_ci``.
6. Minimal columns: the persisted Player carries exactly the Requirement
   16.2 allowlist and no additional columns.
7. Token + id uniqueness: back-to-back registrations produce distinct ids
   and distinct tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence import Player, init_db
from app.services import (
    NicknameTaken,
    NicknameValidationError,
    PlayerService,
    RegistrationSuccess,
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
        # Share one connection across threads for the in-memory DB so
        # the schema persists between session_factory calls.
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # Use a single in-memory database shared between sessions by reusing
    # a StaticPool-like configuration. The default in-memory URL gives a
    # fresh DB per connection; instead we bind a sessionmaker that
    # reuses the same connection via ``poolclass=StaticPool`` indirectly
    # through the engine's default NullPool+check_same_thread=False path
    # for SQLite. To be safe across platforms we materialize schema once
    # and trust that sessionmaker reuses the same engine-level memory DB.
    init_db(eng)
    return eng


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


@pytest.fixture()
def settings() -> Settings:
    # Pin the TTL to a value the tests can reason about without relying
    # on whatever environment the test host has set.
    return Settings(session_ttl_seconds=60)


def _fixed_clock(initial: datetime) -> tuple[callable, callable]:
    """Return ``(clock, advance)`` where ``clock`` is a zero-arg callable
    and ``advance(seconds)`` mutates the pinned time.

    We don't use ``freezegun`` because the real-clock surface we care
    about is just the single function passed into the service; injecting
    it keeps the test hermetic.
    """
    state = {"now": initial}

    def clock() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return clock, advance


def _token_stream(tokens: list[str]) -> callable:
    """Return a callable that dispenses the given tokens in order."""
    it = iter(tokens)

    def factory() -> str:
        return next(it)

    return factory


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_register_happy_path_returns_success_and_persists_expected_row(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    token_factory = _token_stream(["tok-alice"])

    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=token_factory,
    )

    result = service.register("Alice")

    assert isinstance(result, RegistrationSuccess)
    assert result.nickname == "Alice"
    assert result.session_token == "tok-alice"
    assert result.created_at == t0
    assert result.session_expires_at == t0 + timedelta(
        seconds=settings.session_ttl_seconds
    )
    assert result.player_id  # non-empty uuid string
    # player_id is a UUID string of the canonical 36-char form
    assert len(result.player_id) == 36

    # Verify the persisted row mirrors the returned payload exactly.
    # Note: SQLite's ``DateTime(timezone=True)`` column stores values as
    # naive text, so datetimes round-tripped through a fresh session
    # come back naive. Re-attach UTC for equality purposes — the
    # service itself returns aware datetimes from the in-memory Python
    # object it just built, which is what the caller sees.
    with session_factory() as s:
        rows = s.execute(select(Player)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == result.player_id
    assert row.nickname == "Alice"
    assert row.nickname_ci == "alice"
    assert row.session_token == "tok-alice"
    assert row.created_at.replace(tzinfo=timezone.utc) == t0
    assert (
        row.session_expires_at.replace(tzinfo=timezone.utc)
        == result.session_expires_at
    )


# ---------------------------------------------------------------------------
# 2. Length rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "a", "x" * 21, "y" * 50])
def test_register_rejects_out_of_range_length_and_persists_nothing(
    bad: str, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    service = PlayerService(session_factory, settings=settings)

    result = service.register(bad)

    assert isinstance(result, NicknameValidationError)
    assert result.code == "length"
    assert result.details["min"] == 2
    assert result.details["max"] == 20
    assert result.details["actual"] == len(bad)

    with session_factory() as s:
        count = s.execute(select(Player)).scalars().all()
    assert count == []


# ---------------------------------------------------------------------------
# 3. Charset rejection
# ---------------------------------------------------------------------------


def test_register_rejects_disallowed_charset_and_persists_nothing(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    service = PlayerService(session_factory, settings=settings)

    result = service.register("ali@ce")

    assert isinstance(result, NicknameValidationError)
    assert result.code == "charset"
    assert result.details == {"invalid_chars": ["@"]}

    with session_factory() as s:
        rows = s.execute(select(Player)).scalars().all()
    assert rows == []


def test_register_charset_error_reports_each_unique_invalid_char(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    service = PlayerService(session_factory, settings=settings)

    result = service.register("a@b!a?")

    assert isinstance(result, NicknameValidationError)
    assert result.code == "charset"
    # Order = first-occurrence, uniques only.
    assert result.details["invalid_chars"] == ["@", "!", "?"]


# ---------------------------------------------------------------------------
# 4. Duplicate active registration
# ---------------------------------------------------------------------------


def test_register_duplicate_active_is_rejected_case_insensitively(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    token_factory = _token_stream(["tok-first", "tok-second-should-not-be-used"])

    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=token_factory,
    )

    first = service.register("Alice")
    assert isinstance(first, RegistrationSuccess)

    second = service.register("ALICE")
    assert isinstance(second, NicknameTaken)
    assert second.nickname_ci == "alice"

    with session_factory() as s:
        rows = s.execute(select(Player)).scalars().all()
    assert len(rows) == 1
    assert rows[0].nickname == "Alice"  # original casing preserved


# ---------------------------------------------------------------------------
# 5. Duplicate after expiry
# ---------------------------------------------------------------------------


def test_register_after_prior_session_expires_succeeds_and_purges_old_row(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _fixed_clock(t0)
    token_factory = _token_stream(["tok-first", "tok-second"])

    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=token_factory,
    )

    first = service.register("Alice")
    assert isinstance(first, RegistrationSuccess)

    # Advance past the TTL — first player is no longer an Active_Player.
    advance(settings.session_ttl_seconds + 1)

    second = service.register("ALICE")
    assert isinstance(second, RegistrationSuccess), second
    assert second.session_token == "tok-second"
    assert second.nickname == "ALICE"
    assert second.player_id != first.player_id

    with session_factory() as s:
        rows = s.execute(select(Player)).scalars().all()
    # Exactly one Active_Player owns "alice" now: the new one.
    assert len(rows) == 1
    assert rows[0].id == second.player_id
    assert rows[0].nickname_ci == "alice"


# ---------------------------------------------------------------------------
# 6. Minimal columns
# ---------------------------------------------------------------------------


def test_register_persists_only_allowlisted_player_columns(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Requirement 16.2 / Property 20 smoke at the service layer.

    If a future contributor adds a column to the ORM model without
    updating the allowlist, this test flags it regardless of whether
    the service writes to the new column.
    """
    service = PlayerService(
        session_factory,
        settings=settings,
        token_factory=_token_stream(["tok-one"]),
    )
    service.register("Zed")

    allowed = {
        "id",
        "nickname",
        "nickname_ci",
        "created_at",
        "session_token",
        "session_expires_at",
    }
    actual = set(Player.__table__.columns.keys())
    assert actual == allowed


# ---------------------------------------------------------------------------
# 7. Token + id uniqueness across registrations
# ---------------------------------------------------------------------------


def test_consecutive_registrations_produce_distinct_ids_and_tokens(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    # Use the real token_factory default (secrets.token_urlsafe); it
    # produces distinct values with overwhelming probability. We also
    # rely on uuid4 for id uniqueness.
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    service = PlayerService(session_factory, settings=settings, clock=clock)

    a = service.register("Alice")
    b = service.register("Bob")

    assert isinstance(a, RegistrationSuccess)
    assert isinstance(b, RegistrationSuccess)
    assert a.player_id != b.player_id
    assert a.session_token != b.session_token
    # Both still within the TTL window.
    assert a.session_expires_at == t0 + timedelta(
        seconds=settings.session_ttl_seconds
    )
    assert b.session_expires_at == a.session_expires_at
