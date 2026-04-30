"""Deterministic unit tests for ``PlayerService.authorize`` (task 3.2).

These tests pin the clock and the token_factory so every assertion is
reproducible without Hypothesis — the property-based authorization test
is task 3.4 and lives in its own file.

Covers:

1. Happy path: a freshly-registered token authorizes to its player.
2. Missing token: ``None``, empty string, and whitespace-only strings
   all surface as ``Unauthorized(reason="missing")`` and never touch
   the DB-visible state.
3. Unknown token: a well-formed but unrecognized token surfaces as
   ``Unauthorized(reason="unknown")``.
4. Expired token: advancing the clock past the TTL causes the same
   token that just authorized to be rejected as ``expired``.
5. Boundary — exactly at expiry: ``now == session_expires_at`` is
   treated as expired (strict inequality on the expiry side).
6. Boundary — one second before expiry: still authorized.
7. Two players, distinct tokens: each token authorizes to its own
   player; they don't cross-match.
8. No sliding expiry: authorizing twice returns the same
   ``session_expires_at`` — the service doesn't mutate the row.
9. Original nickname casing is preserved on authorize (Requirement 1.3).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence import init_db
from app.services import (
    AuthorizedPlayer,
    PlayerService,
    RegistrationSuccess,
    Unauthorized,
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


@pytest.fixture()
def settings() -> Settings:
    return Settings(session_ttl_seconds=60)


def _fixed_clock(initial: datetime) -> tuple[callable, callable]:
    """Return ``(clock, advance)`` where ``clock`` is a zero-arg callable
    and ``advance(seconds)`` moves the pinned time forward."""
    state = {"now": initial}

    def clock() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return clock, advance


def _set_clock(initial: datetime) -> tuple[callable, callable]:
    """Variant of :func:`_fixed_clock` that can also jump to an absolute time."""
    state = {"now": initial}

    def clock() -> datetime:
        return state["now"]

    def set_to(target: datetime) -> None:
        state["now"] = target

    return clock, set_to


def _token_stream(tokens: list[str]) -> callable:
    """Return a callable that dispenses the given tokens in order."""
    it = iter(tokens)

    def factory() -> str:
        return next(it)

    return factory


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_authorize_happy_path_returns_authorized_player(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )

    reg = service.register("Alice")
    assert isinstance(reg, RegistrationSuccess)

    result = service.authorize("tok-alice")

    assert isinstance(result, AuthorizedPlayer)
    assert result.player_id == reg.player_id
    assert result.nickname == "Alice"
    assert result.session_expires_at == t0 + timedelta(
        seconds=settings.session_ttl_seconds
    )
    # Must be timezone-aware UTC.
    assert result.session_expires_at.tzinfo is not None
    assert result.session_expires_at.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# 2. Missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   ", "\t", "\n", " \t\n "])
def test_authorize_missing_token_returns_missing(
    bad,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    service = PlayerService(session_factory, settings=settings)

    result = service.authorize(bad)

    assert isinstance(result, Unauthorized)
    assert result.reason == "missing"


# ---------------------------------------------------------------------------
# 3. Unknown token
# ---------------------------------------------------------------------------


def test_authorize_unknown_token_returns_unknown(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )

    # Registering ensures there's at least one row in the DB, so
    # "unknown" isn't conflated with "empty table".
    assert isinstance(service.register("Alice"), RegistrationSuccess)

    result = service.authorize("not-a-real-token")

    assert isinstance(result, Unauthorized)
    assert result.reason == "unknown"


def test_authorize_unknown_token_on_empty_db(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    service = PlayerService(session_factory, settings=settings)

    result = service.authorize("no-such-token")

    assert isinstance(result, Unauthorized)
    assert result.reason == "unknown"


# ---------------------------------------------------------------------------
# 4. Expired token
# ---------------------------------------------------------------------------


def test_authorize_expired_token_returns_expired(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )
    assert isinstance(service.register("Alice"), RegistrationSuccess)

    # Jump well past the TTL.
    advance(settings.session_ttl_seconds + 1)

    result = service.authorize("tok-alice")

    assert isinstance(result, Unauthorized)
    assert result.reason == "expired"


# ---------------------------------------------------------------------------
# 5 / 6. Expiry boundary
# ---------------------------------------------------------------------------


def test_authorize_exactly_at_expiry_is_expired(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, set_to = _set_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )
    reg = service.register("Alice")
    assert isinstance(reg, RegistrationSuccess)

    # Pin the clock to the exact expiry instant.
    set_to(reg.session_expires_at)

    result = service.authorize("tok-alice")

    assert isinstance(result, Unauthorized)
    assert result.reason == "expired"


def test_authorize_one_second_before_expiry_is_authorized(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, set_to = _set_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )
    reg = service.register("Alice")
    assert isinstance(reg, RegistrationSuccess)

    # One second shy of the expiry instant.
    set_to(reg.session_expires_at - timedelta(seconds=1))

    result = service.authorize("tok-alice")

    assert isinstance(result, AuthorizedPlayer)
    assert result.player_id == reg.player_id
    assert result.session_expires_at == reg.session_expires_at


# ---------------------------------------------------------------------------
# 7. Two players, distinct tokens
# ---------------------------------------------------------------------------


def test_authorize_distinguishes_between_two_players(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice", "tok-bob"]),
    )
    alice = service.register("Alice")
    bob = service.register("Bob")
    assert isinstance(alice, RegistrationSuccess)
    assert isinstance(bob, RegistrationSuccess)

    a = service.authorize("tok-alice")
    b = service.authorize("tok-bob")

    assert isinstance(a, AuthorizedPlayer)
    assert isinstance(b, AuthorizedPlayer)
    assert a.player_id == alice.player_id
    assert a.nickname == "Alice"
    assert b.player_id == bob.player_id
    assert b.nickname == "Bob"
    assert a.player_id != b.player_id


# ---------------------------------------------------------------------------
# 8. No sliding expiry
# ---------------------------------------------------------------------------


def test_authorize_does_not_mutate_session_expires_at(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )
    reg = service.register("Alice")
    assert isinstance(reg, RegistrationSuccess)

    first = service.authorize("tok-alice")
    advance(1)  # well inside the window
    second = service.authorize("tok-alice")

    assert isinstance(first, AuthorizedPlayer)
    assert isinstance(second, AuthorizedPlayer)
    # Expiry is fixed at issuance — Requirement 7.5.
    assert first.session_expires_at == reg.session_expires_at
    assert second.session_expires_at == reg.session_expires_at


# ---------------------------------------------------------------------------
# 9. Original nickname casing preserved
# ---------------------------------------------------------------------------


def test_authorize_returns_original_nickname_casing(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _fixed_clock(t0)
    service = PlayerService(
        session_factory,
        settings=settings,
        clock=clock,
        token_factory=_token_stream(["tok-alice"]),
    )
    assert isinstance(service.register("Alice"), RegistrationSuccess)

    result = service.authorize("tok-alice")

    assert isinstance(result, AuthorizedPlayer)
    # Case-folded for uniqueness, but the displayed nickname keeps its
    # original casing per Requirement 1.3.
    assert result.nickname == "Alice"
