"""Property-based tests for registration output well-formedness (task 3.3).

**Property 2: Successful registration produces a well-formed Player and
bound Session_Token.**

**Validates: Requirements 1.3, 7.1.**

For any accepted nickname ``n`` registered against an empty database,
the produced :class:`~app.services.RegistrationSuccess` ``r`` must
satisfy ALL of:

1. ``r.player_id`` is a non-empty string in the canonical UUIDv4 form
   (36 chars with hyphens, parses via :class:`uuid.UUID`, version 4).
2. ``r.nickname == n`` — original casing preserved verbatim (neither
   stripped nor case-folded).
3. ``r.created_at`` is a timezone-aware UTC ``datetime`` equal to the
   injected ``clock()`` value at issuance.
4. ``r.session_token`` equals the value dispensed by the injected
   ``token_factory`` and is a non-empty string.
5. ``service.authorize(r.session_token)`` returns an
   :class:`~app.services.AuthorizedPlayer` whose ``player_id`` equals
   ``r.player_id`` — the token resolves back to exactly the new Player
   (Requirement 7.1: token bound to exactly one ``playerId``).
6. The persisted row visible through a fresh session has
   ``id == r.player_id``, ``nickname == n``, ``nickname_ci ==
   n.casefold()``, and ``session_token == r.session_token``
   (triangulates the in-memory response with the DB state).

A companion property covers clause 7:

7. Registering a second, differently-cased nickname against the same
   service produces a :class:`~app.services.RegistrationSuccess` whose
   ``player_id`` and ``session_token`` are both distinct from the
   first, confirming id + token uniqueness across calls.

Out of scope here:
- Format-level nickname validation (covered by
  ``tests/test_property_nickname.py``).
- Session expiry and authorize-failure modes (task 3.4).
- Any production-code change — this module is pure test code.
"""

from __future__ import annotations

import itertools
import string
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence import Player, init_db
from app.services import (
    AuthorizedPlayer,
    PlayerService,
    RegistrationSuccess,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------
#
# Guarded registration mirrors the pattern in
# ``tests/test_property_nickname.py`` so reloads under pytest don't
# raise ``InvalidArgument`` on a duplicate profile name. ``deadline=None``
# keeps slow CI machines from tripping per-example deadlines when each
# example builds a fresh in-memory SQLite engine + schema.

try:
    settings.register_profile(
        "player-register-property",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("player-register-property")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# Reuses the allowed-alphabet + length-range technique from
# ``test_property_nickname.py`` so every generated nickname is
# guaranteed to satisfy Requirements 1.5 and 1.6 — this property is
# about the OUTPUT of a successful registration, not the format rules.

_ALLOWED_ALPHABET: list[str] = list(
    string.ascii_letters + string.digits + " _-"
)

_valid_nickname = st.text(
    alphabet=st.sampled_from(_ALLOWED_ALPHABET),
    min_size=2,
    max_size=20,
)


@st.composite
def _two_distinct_valid_nicknames(draw: st.DrawFn) -> tuple[str, str]:
    """Two valid nicknames whose casefolded forms differ.

    The casefold-level distinction matters because a same-casefold pair
    against the same service would be rejected as an Active_Player
    collision on the second call (Requirement 1.7), which would
    invalidate the "distinct ids" property under test.
    """
    a = draw(_valid_nickname)
    b = draw(_valid_nickname.filter(lambda x: x.casefold() != a.casefold()))
    return a, b


# ---------------------------------------------------------------------------
# Per-example service builder
# ---------------------------------------------------------------------------
#
# Each Hypothesis example needs a fresh, empty DB so prior examples'
# rows don't pollute the ``nickname_ci`` unique constraint. Building
# the engine + sessionmaker + service inside the test body (rather
# than as a pytest fixture) guarantees isolation regardless of how
# Hypothesis re-uses fixture instances across examples.


def _build_fresh_service(
    clock_value: datetime,
    tokens: list[str],
) -> tuple[PlayerService, sessionmaker[Session], Callable[[], str]]:
    """Create a per-example PlayerService against an empty in-memory DB.

    Args:
        clock_value: Fixed timezone-aware UTC datetime the injected
            clock returns on every call. Pinning the clock lets the
            property assert exact equality on ``created_at`` rather
            than an epsilon-tolerant comparison.
        tokens: Pre-computed tokens the ``token_factory`` will dispense
            in order. Each must be a distinct non-empty string so
            assertions comparing against a specific token value hold.

    Returns:
        ``(service, session_factory, token_factory)`` — the caller uses
        ``session_factory`` for DB-state triangulation and retains a
        reference to ``token_factory`` only for documentation; the
        service already owns it.
    """
    engine: Engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    def clock() -> datetime:
        return clock_value

    token_iter = iter(tokens)

    def token_factory() -> str:
        return next(token_iter)

    service = PlayerService(
        session_factory,
        settings=Settings(session_ttl_seconds=60),
        clock=clock,
        token_factory=token_factory,
    )
    return service, session_factory, token_factory


# Pinned clock constant — the property isn't trying to prove behaviour
# across many clock values (that's the domain of task 3.4's session-
# expiry properties). A single fixed instant gives us exact-equality
# assertions on ``created_at``.
_FIXED_CLOCK = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Deterministic token stream. Using ``itertools.count`` produces
# unique, observable token values so assertion 4 can compare exact
# equality. A module-level counter would bleed across examples — we
# build a fresh counter inside each example instead.
def _fresh_token_for(label: str) -> str:
    """Deterministic unique token for an example, seeded from a counter."""
    return f"tok-{label}-{next(_token_counter)}"


_token_counter = itertools.count()


# ---------------------------------------------------------------------------
# Test A — single-registration well-formedness
# ---------------------------------------------------------------------------


@given(nickname=_valid_nickname)
@settings(max_examples=100, deadline=None)
def test_successful_registration_is_well_formed(nickname: str) -> None:
    """Property 2 clauses 1–6: a successful registration is well-formed.

    Covers:
    - Clause 1: ``player_id`` is a canonical UUIDv4 string.
    - Clause 2: ``nickname`` is preserved verbatim (no strip / casefold).
    - Clause 3: ``created_at`` equals the injected clock, is UTC.
    - Clause 4: ``session_token`` equals the dispensed token.
    - Clause 5: ``authorize(session_token).player_id == player_id``.
    - Clause 6: the persisted row agrees with the returned payload.

    Validates: Requirements 1.3, 7.1.
    """
    expected_token = f"tok-A-{next(_token_counter)}"
    service, session_factory, _ = _build_fresh_service(
        clock_value=_FIXED_CLOCK,
        tokens=[expected_token],
    )

    result = service.register(nickname)

    # Clause preamble: registration accepted. Any generated nickname
    # satisfies Requirements 1.5 / 1.6 and the DB is empty, so this
    # must succeed; a failure here would be a bug in the service
    # rather than the property, so we assert it rather than assume().
    assert isinstance(result, RegistrationSuccess), result

    # --- Clause 1: canonical UUIDv4 player_id --------------------------
    assert isinstance(result.player_id, str)
    assert len(result.player_id) == 36
    parsed = uuid.UUID(result.player_id)  # raises on malformed
    assert parsed.version == 4
    # Canonical lowercase hex representation.
    assert str(parsed) == result.player_id

    # --- Clause 2: nickname preserved verbatim -------------------------
    assert result.nickname == nickname

    # --- Clause 3: created_at equals the clock, is UTC -----------------
    from datetime import timedelta as _td  # local alias for clarity

    assert result.created_at == _FIXED_CLOCK
    assert result.created_at.tzinfo is not None
    assert result.created_at.utcoffset() == _td(0)

    # --- Clause 4: session_token equals what the factory dispensed -----
    assert result.session_token == expected_token
    assert isinstance(result.session_token, str)
    assert result.session_token  # non-empty

    # --- Clause 5: authorize(token).player_id == player_id -------------
    auth = service.authorize(result.session_token)
    assert isinstance(auth, AuthorizedPlayer), auth
    assert auth.player_id == result.player_id
    # And the nickname round-trips through authorize too, for good measure.
    assert auth.nickname == nickname

    # --- Clause 6: DB row agrees with the returned payload -------------
    with session_factory() as s:
        rows = s.execute(select(Player)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == result.player_id
    assert row.nickname == nickname
    assert row.nickname_ci == nickname.casefold()
    assert row.session_token == result.session_token


# ---------------------------------------------------------------------------
# Test B — distinct registrations have distinct ids and tokens
# ---------------------------------------------------------------------------


@given(pair=_two_distinct_valid_nicknames())
@settings(max_examples=50, deadline=None)
def test_distinct_registrations_have_distinct_ids(pair: tuple[str, str]) -> None:
    """Property 2 clause 7: distinct registrations get distinct identities.

    Registering two nicknames with different casefolded forms against
    the same service produces two :class:`RegistrationSuccess` results
    whose ``player_id`` values are distinct (id uniqueness across
    calls) and whose ``session_token`` values are distinct (one token
    per player, Requirement 7.1).

    Validates: Requirements 1.3, 7.1.
    """
    a, b = pair
    token_a = f"tok-B-a-{next(_token_counter)}"
    token_b = f"tok-B-b-{next(_token_counter)}"
    service, _, _ = _build_fresh_service(
        clock_value=_FIXED_CLOCK,
        tokens=[token_a, token_b],
    )

    first = service.register(a)
    second = service.register(b)

    assert isinstance(first, RegistrationSuccess), first
    assert isinstance(second, RegistrationSuccess), second
    assert first.player_id != second.player_id
    assert first.session_token != second.session_token
    # Token-factory dispensed exactly the values we supplied.
    assert first.session_token == token_a
    assert second.session_token == token_b
