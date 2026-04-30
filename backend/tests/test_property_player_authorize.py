"""Property-based test for session-token authorization (task 3.4).

**Property 3: Session token authorization on protected endpoints.**

**Validates: Requirements 7.2, 7.3, 7.5.**

Over arbitrary interleavings of three event types — register a new
player, advance the clock, and "revoke" (force-expire) an existing
player's session — the service's ``authorize`` decision must match a
reference decision computed from an independent model of the
registered-players state. The bi-implication is:

``service.authorize(token)`` returns ``Unauthorized`` if and only if at
least one of the following holds at the moment of the call:

* ``token is None`` or its stripped form is empty → ``missing``;
* ``token`` is a non-empty string that doesn't exactly match any row
  currently present in the DB → ``unknown``;
* ``token`` matches some row, but that row's ``session_expires_at <=
  clock()`` at call time → ``expired``.

Otherwise, ``authorize`` returns an ``AuthorizedPlayer`` whose
``player_id`` and ``nickname`` match the row that issued the token.

Modelling "revoke"
------------------

The spec has no ``revoke`` endpoint in v1 — ``PlayerService`` exposes
only ``register`` and ``authorize``. The task's "revoke events" are
therefore modelled as **force-expire by clock-jump**: a dedicated rule
picks one live, known token and advances the clock to
``entry.expires_at + 1 second``. After such a rule fires, the
reference model classifies that token as ``expired`` (or ``unknown``,
if the service purged the underlying row as part of a later collision
— see below) and the property asserts ``authorize`` agrees.

Collision-purge interaction
---------------------------

``PlayerService.register`` collapses the rare case where a new
nickname collides with an *expired* existing row by deleting the
expired row before inserting the new one. That means the old token is
no longer in the DB and ``authorize`` will classify it as ``unknown``
rather than ``expired``. The model mirrors this by evicting any
expired model entries whose ``nickname_ci`` matches the nickname of a
new registration, so the reference decision stays consistent with the
service's behaviour.

Strategy
--------

Uses :class:`hypothesis.stateful.RuleBasedStateMachine` — the idiomatic
tool for "a sequence of events". Each example builds a fresh in-memory
SQLite engine + ``PlayerService`` in :py:meth:`@initialize()`, then
explores an arbitrary sequence of register / advance-clock /
force-expire / authorize-* rules. The property is checked both inside
each authorize-* rule (pointwise) and as an ``@invariant`` on every
known live token after every rule (so the bi-implication holds at all
observable points, not only on authorize calls).

Out of scope here
-----------------

- Format-level nickname validation (task 2.3's property test owns it).
- Registration output well-formedness (task 3.3's property test owns it).
- Any production-code change — this module is pure test code.
"""

from __future__ import annotations

import itertools
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.persistence import init_db
from app.services import (
    AuthorizedPlayer,
    NicknameTaken,
    NicknameValidationError,
    PlayerService,
    RegistrationSuccess,
    Unauthorized,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------
#
# Same guarded pattern as the other PBT modules so re-imports don't
# raise ``InvalidArgument``. ``print_blob=True`` surfaces a copyable
# reproducer on failure and ``deadline=None`` avoids flaky deadline
# trips when building a fresh in-memory engine per example.

try:
    settings.register_profile(
        "player-authorize-property",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("player-authorize-property")


# ---------------------------------------------------------------------------
# Strategies and constants
# ---------------------------------------------------------------------------

# Small round TTL — each example advances the clock multiple times,
# and a small TTL makes the expired-branch hit frequently without
# inflating test wall-clock time.
_TTL_SECONDS: int = 60

# A fixed starting instant. All clock arithmetic in the machine is
# relative to this; using a single epoch keeps shrunk counterexamples
# compact.
_EPOCH: datetime = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Allowed nickname alphabet — same as the format-level rules (1.5/1.6)
# so every register rule draws a format-valid nickname. Casefold
# collisions can still occur (e.g. "Alice" vs "alice"); the register
# rule handles them by skipping the registration.
_ALLOWED_ALPHABET: list[str] = list(
    string.ascii_letters + string.digits + " _-"
)

_valid_nickname = st.text(
    alphabet=st.sampled_from(_ALLOWED_ALPHABET),
    min_size=2,
    max_size=20,
)

# "Missing"-class strings that must be rejected with reason=missing
# without a DB round-trip. The service strips before emptiness-check,
# so any whitespace-only string is in this bucket.
_missing_strings = st.sampled_from(["", " ", "   ", "\t", "\n", " \t\n "])

# Arbitrary text for unknown-token probing. Bounded length keeps
# shrinking tractable. No alphabet restriction: the service treats
# tokens as opaque bytes-like strings, so any input is fair.
_arbitrary_text = st.text(min_size=1, max_size=60)


# ---------------------------------------------------------------------------
# Reference model
# ---------------------------------------------------------------------------


@dataclass
class _ModelEntry:
    """Reference snapshot of one registered Player for the property.

    Mirrors only the subset of the persisted row the property cares
    about. Kept plain and mutable for simplicity; the state machine
    never shares entries across examples.
    """

    player_id: str
    nickname: str
    token: str
    expires_at: datetime  # aware UTC


# ---------------------------------------------------------------------------
# Per-example service builder
# ---------------------------------------------------------------------------


def _build_fresh_service(
    clock: Callable[[], datetime],
    token_factory: Callable[[], str],
    ttl_seconds: int,
) -> tuple[PlayerService, sessionmaker[Session]]:
    """Create a ``PlayerService`` against an empty in-memory SQLite DB.

    Foreign keys are enabled explicitly (SQLite default is off) to
    match how the production engine is configured. Each example gets
    its own engine + sessionmaker so registrations from prior examples
    do not leak into the current one.
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
    service = PlayerService(
        session_factory,
        settings=Settings(session_ttl_seconds=ttl_seconds),
        clock=clock,
        token_factory=token_factory,
    )
    return service, session_factory


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TokenAuthorizationMachine(RuleBasedStateMachine):
    """Stateful test driving Property 3 across arbitrary event sequences.

    State owned by each example:

    - ``service`` — a fresh :class:`PlayerService` over an empty
      in-memory SQLite engine.
    - ``clock_value`` — the pinned "current time"; read through the
      closure injected into ``service``.
    - ``ttl_seconds`` — fixed per example, small enough that expiry
      happens frequently during exploration.
    - ``model`` — ``dict[token, _ModelEntry]`` keyed by token. Rows
      that the service purges on collision are evicted here too, so
      the reference decision matches the service's behaviour.
    - ``live_tokens`` Bundle — accumulates tokens from successful
      registrations so later rules can target them specifically.

    The pointwise property assertion lives in :meth:`_check_token`,
    which is called directly from the authorize-* rules and also from
    the :meth:`check_all_known_tokens` invariant. This double-check
    ensures consistency holds at every observable point — not only
    immediately after an authorize call.
    """

    live_tokens: Bundle[str] = Bundle("live_tokens")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @initialize()
    def _setup(self) -> None:
        """Build the per-example service, model, and injected clock/tokens."""
        # Pinned clock readable via closure — rules mutate
        # ``self.clock_value`` and the next ``clock()`` call observes it.
        self.clock_value: datetime = _EPOCH
        self.ttl_seconds: int = _TTL_SECONDS

        def clock() -> datetime:
            return self.clock_value

        # Deterministic, unique-per-instance token factory. Shared
        # across rules via the service; each call returns a distinct
        # string so no two registrations collide on token.
        counter = itertools.count()

        def token_factory() -> str:
            return f"tok-state-{next(counter)}"

        self.service, self.session_factory = _build_fresh_service(
            clock=clock,
            token_factory=token_factory,
            ttl_seconds=self.ttl_seconds,
        )
        self.model: dict[str, _ModelEntry] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _expected_result(self, token: str | None) -> tuple:
        """Reference decision for ``authorize(token)``, computed from the model.

        Returns one of:
        - ``("unauthorized", "missing")``
        - ``("unauthorized", "unknown")``
        - ``("unauthorized", "expired")``
        - ``("authorized", player_id, nickname)``

        The branches match ``PlayerService.authorize`` exactly:

        1. ``None`` → missing.
        2. stripped == "" → missing.
        3. stripped not in model → unknown.
        4. ``entry.expires_at <= clock_value`` → expired.
        5. otherwise → authorized with ``(player_id, nickname)``.
        """
        if token is None:
            return ("unauthorized", "missing")
        stripped = token.strip()
        if stripped == "":
            return ("unauthorized", "missing")
        entry = self.model.get(stripped)
        if entry is None:
            return ("unauthorized", "unknown")
        if entry.expires_at <= self.clock_value:
            return ("unauthorized", "expired")
        return ("authorized", entry.player_id, entry.nickname)

    def _check_token(self, token: str | None) -> None:
        """Assert the service's decision on ``token`` matches the model's."""
        expected = self._expected_result(token)
        actual = self.service.authorize(token)

        if expected[0] == "unauthorized":
            assert isinstance(actual, Unauthorized), (
                f"expected Unauthorized({expected[1]}), got {actual!r}"
            )
            assert actual.reason == expected[1], (
                f"expected reason={expected[1]}, got {actual.reason} "
                f"for token={token!r}"
            )
        else:
            _, expected_player_id, expected_nickname = expected
            assert isinstance(actual, AuthorizedPlayer), (
                f"expected AuthorizedPlayer, got {actual!r}"
            )
            assert actual.player_id == expected_player_id, (
                f"player_id mismatch on token={token!r}"
            )
            assert actual.nickname == expected_nickname, (
                f"nickname mismatch on token={token!r}"
            )

    # ------------------------------------------------------------------
    # Event rules
    # ------------------------------------------------------------------

    @rule(target=live_tokens, nickname=_valid_nickname)
    def register(self, nickname: str) -> str:
        """Register a fresh nickname and record the outcome in the model.

        Three outcomes are possible:

        * :class:`RegistrationSuccess` — the common path. Record the
          new entry in ``model`` keyed by the issued token, and add
          the token to the ``live_tokens`` bundle so later rules can
          pick it. Also evict any prior model entries whose
          ``nickname_ci`` matches the new nickname AND whose stored
          ``expires_at <= now``: the service purges the underlying
          DB row in that case, so the old token becomes ``unknown``
          rather than ``expired``.

        * :class:`NicknameTaken` — the generated nickname collides
          casefold-wise with a still-active model entry. Skip the
          bundle assignment by returning a sentinel that Hypothesis
          will filter out via ``multiple(value)`` semantics — here
          we return a string that won't be put in ``live_tokens``.
          Actually, ``@rule(target=bundle)`` requires returning a
          value for the bundle, so we fall back to returning the
          sentinel ``multiple()`` pattern via hypothesis...

          Correction: to skip adding to the bundle we must NOT return
          from a ``target=`` rule. We restructure: the rule always
          returns ``hypothesis.stateful.multiple(...)``, which adds
          zero or one element to the bundle depending on outcome.

        * :class:`NicknameValidationError` — every generated nickname
          comes from the allowed alphabet and length range, so this
          branch should be unreachable. If it fires, it indicates a
          strategy/validator drift and is surfaced as an assertion
          failure rather than silently skipped.
        """
        from hypothesis.stateful import multiple

        now = self.clock_value
        # Pre-compute casefold to mirror the service's uniqueness key.
        nickname_ci = nickname.casefold()

        # Evict model entries that the service WILL purge on collision:
        # same nickname_ci AND already expired at the current clock.
        to_evict = [
            t
            for t, entry in self.model.items()
            if entry.nickname.casefold() == nickname_ci
            and entry.expires_at <= now
        ]
        # We evict *optimistically* only after we know the service
        # accepted the new registration (it might still be rejected
        # due to an active collision). Capture the candidates here;
        # apply below.

        result = self.service.register(nickname)

        if isinstance(result, RegistrationSuccess):
            # Service purged any expired colliding row — mirror that.
            for t in to_evict:
                self.model.pop(t, None)
            self.model[result.session_token] = _ModelEntry(
                player_id=result.player_id,
                nickname=result.nickname,
                token=result.session_token,
                expires_at=result.session_expires_at,
            )
            return multiple(result.session_token)

        if isinstance(result, NicknameTaken):
            # An active player already owns this nickname_ci. The
            # model already reflects that — no change, no bundle add.
            return multiple()

        if isinstance(result, NicknameValidationError):
            # Must not happen given the strategy. Surface as a failure.
            raise AssertionError(
                f"strategy drift: generated nickname {nickname!r} was rejected "
                f"with {result!r} — the strategy must only produce "
                f"format-valid nicknames."
            )

        raise AssertionError(f"unexpected register result: {result!r}")

    @rule(seconds=st.integers(min_value=0, max_value=_TTL_SECONDS * 3))
    def advance_clock(self, seconds: int) -> None:
        """Advance the pinned clock by ``seconds``.

        Range spans from zero (no-op, useful for shrinking) up to
        three TTLs, guaranteeing that advancing repeatedly will
        straddle expiry boundaries for any previously-registered
        player. Does not touch the service or the DB.
        """
        self.clock_value = self.clock_value + timedelta(seconds=seconds)

    @rule(token=live_tokens)
    def revoke(self, token: str) -> None:
        """Force-expire a known live token by clock-jump.

        Interpretation: the task's "revoke" is modelled as an
        operator-driven clock advance that pushes the system past
        this specific session's ``session_expires_at``. After this
        rule, ``authorize(token)`` must return Unauthorized with
        reason ``expired`` (assuming the row is still in the DB and
        has not been purged by a later collision).

        If the token has already been evicted from the model (e.g.,
        purged by a later register collision), this rule is a no-op
        on the model — the clock jump below is still recorded, since
        it changes the global clock state.
        """
        entry = self.model.get(token)
        if entry is None:
            # Token was purged by a register-collision; nothing to
            # force-expire specifically. Advance the clock by a
            # token-sized step anyway to keep the rule observable.
            self.clock_value = self.clock_value + timedelta(seconds=1)
            return
        # Jump to one second past this entry's expiry — monotonic:
        # never roll the clock backwards.
        target = entry.expires_at + timedelta(seconds=1)
        if target > self.clock_value:
            self.clock_value = target

    @rule(token=live_tokens)
    def authorize_known(self, token: str) -> None:
        """Call ``authorize`` on a token we've seen at least once.

        This token may be:
        - still live (clock < expires_at) → expected authorized;
        - expired (clock >= expires_at) → expected Unauthorized/expired;
        - already purged by a collision → expected Unauthorized/unknown.

        The expected decision is computed entirely from the model.
        """
        self._check_token(token)

    @rule()
    def authorize_none(self) -> None:
        """``authorize(None)`` must always return Unauthorized/missing."""
        self._check_token(None)

    @rule(s=_missing_strings)
    def authorize_missing_string(self, s: str) -> None:
        """Empty/whitespace-only strings must return Unauthorized/missing.

        ``secrets.token_urlsafe`` never emits whitespace, so a
        whitespace payload is guaranteed not to match any issued
        token — the service short-circuits this case without a DB
        query, and the model classifies it as ``missing``.
        """
        self._check_token(s)

    @rule(candidate=_arbitrary_text)
    def authorize_arbitrary(self, candidate: str) -> None:
        """``authorize`` on an arbitrary string: unknown or (rarely) known.

        Hypothesis will most often generate strings that don't match
        any issued token (exercising the ``unknown`` branch), but
        will occasionally generate one that coincidentally equals a
        live token (exercising ``authorized``/``expired``). Either
        way, the reference decision is computed from the model and
        compared to the service.
        """
        self._check_token(candidate)

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    @invariant()
    def check_all_known_tokens(self) -> None:
        """After every rule, every known token must satisfy the property.

        Catches inconsistencies that a pointwise authorize-rule might
        miss (e.g., a token that silently transitions between
        classifications without any authorize-rule having fired since
        the transition).
        """
        for token in list(self.model.keys()):
            self._check_token(token)


# ---------------------------------------------------------------------------
# Pytest entry point
# ---------------------------------------------------------------------------
#
# Hypothesis exposes a ``TestCase`` attribute on each state-machine
# class that pytest can discover. Wrapping it with a ``TestX`` name
# makes the test appear in the suite as a single test that runs many
# Hypothesis examples. ``max_examples`` × ``stateful_step_count``
# is tuned to keep total wall-clock comfortably under ~30s on CI.

TestTokenAuthorizationMachine = TokenAuthorizationMachine.TestCase
TestTokenAuthorizationMachine.settings = settings(
    max_examples=40,
    stateful_step_count=25,
    deadline=None,
    print_blob=True,
    suppress_health_check=[
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
    ],
)
