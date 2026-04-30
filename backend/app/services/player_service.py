"""Player_Service implementation (task 3.1 + 3.2).

This module owns the write-path for registration and the read-path for
session-token authorization. The pure format-level rules for nicknames
live in :mod:`app.domain.nickname`; this service composes them with the
Active_Player uniqueness rule (Requirement 1.7) and the session-token
issuance policy (Requirements 7.1 and 7.5).

Design notes:

- **Allowlist of persisted fields.** The service writes exactly the
  columns declared on :class:`app.persistence.models.Player`
  (Requirement 16.2 / Property 20): ``id``, ``nickname``, ``nickname_ci``,
  ``created_at``, ``session_token``, ``session_expires_at``.
- **Uniqueness is scoped to Active_Players.** The SQL ``UNIQUE`` on
  ``players.nickname_ci`` is strict and does not know about session
  expiry, but Requirement 1.7 only blocks a nickname while its owner is
  still an Active_Player (session not expired). Registering a nickname
  whose previous holder's session has expired must therefore succeed.
  The service enforces this by (a) looking up active collisions
  manually before the insert, and (b) on an ``IntegrityError`` from the
  unique constraint — which can only come from a race with an expired
  row that was not yet purged, or a race between two concurrent
  registrations of the same nickname — deleting the expired row and
  retrying exactly once. If the retry collision is with an active
  player, we surface :class:`NicknameTaken` instead of propagating the
  ``IntegrityError`` so the caller sees a stable conflict error.
- **Session tokens** are generated via :func:`secrets.token_urlsafe`
  (opaque, URL-safe, ~43 chars for a 32-byte seed — fits the 64-char
  column). Not ``uuid4``: UUIDs are structured and have lower entropy.
- **Clock injection.** The service accepts a ``clock`` callable so tests
  can pin time. Default is ``datetime.now(timezone.utc)`` so stored
  timestamps are always timezone-aware (consistent with the ORM
  ``DateTime(timezone=True)`` columns).
- **Per-call sessions.** We open a DB session inside each public method
  via ``session_factory()`` rather than holding one on the service
  instance. That keeps the service stateless and therefore safe to
  share across threads / FastAPI workers.

Requirements addressed:
- 1.3  (Successful registration creates a Player with id, nickname,
       createdAt, sessionToken and the API returns playerId + token)
- 1.5, 1.6, 1.7 (Nickname format + case-insensitive Active_Player
                uniqueness — delegated in part to the domain validator)
- 7.1  (Session_Token is bound to exactly one playerId at registration)
- 7.5  (Session_Token has a bounded lifetime)
- 16.1, 16.2 (Persist only allowlisted Player fields)
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..domain.nickname import (
    CharsetError,
    LengthError,
    Ok,
    validate_nickname,
)
from ..persistence.models import Player


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationSuccess:
    """A successful registration outcome.

    Mirrors the shape of the persisted :class:`Player` row, without
    exposing the ORM instance itself so callers can't accidentally
    mutate it after the session closes.
    """

    player_id: str
    session_token: str
    nickname: str
    created_at: datetime
    session_expires_at: datetime


@dataclass(frozen=True)
class NicknameValidationError:
    """Format-level rejection, flattened from the domain validator.

    The API layer maps this to HTTP 400. ``code`` is a stable
    machine-readable discriminator; ``details`` is the structured
    payload tailored to ``code`` (length bounds or offending chars).
    """

    code: Literal["length", "charset"]
    details: dict


@dataclass(frozen=True)
class NicknameTaken:
    """Another Active_Player already owns this case-folded nickname.

    The API layer maps this to HTTP 409 (Requirement 1.7 / 1.8).
    """

    nickname_ci: str


RegistrationResult = RegistrationSuccess | NicknameValidationError | NicknameTaken


@dataclass(frozen=True)
class AuthorizedPlayer:
    """Outcome of a successful Session_Token authorization.

    Mirrors the subset of Player state the API layer needs to enforce
    per-request authorization without re-querying. ``session_expires_at``
    is returned as timezone-aware UTC.
    """

    player_id: str
    nickname: str
    session_expires_at: datetime


@dataclass(frozen=True)
class Unauthorized:
    """Failure outcome of a Session_Token authorization.

    ``reason`` is a machine-readable discriminator so the API layer
    (and ops tooling) can log / monitor authorization failures. The
    HTTP response body must remain generic per Requirement 7.3 — we
    do not leak which half of the check failed to clients.
    """

    reason: Literal["missing", "unknown", "expired"]


AuthorizationResult = AuthorizedPlayer | Unauthorized


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Timezone-aware UTC clock — matches ``DateTime(timezone=True)``."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite's ``DateTime(timezone=True)`` column stores values as text
    without tzinfo, so round-tripped datetimes come back naive even
    though we wrote them as aware. Treat any naive value as already-UTC
    rather than rejecting comparisons at runtime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _default_token_factory() -> str:
    """Opaque URL-safe session token (Requirement 7.1 / 7.5).

    32 bytes of entropy → ~43 chars of base64-url, comfortably under
    the 64-char column limit on ``players.session_token``.
    """
    return secrets.token_urlsafe(32)


class PlayerService:
    """Registration and session-token authorization for players."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = _default_clock,
        token_factory: Callable[[], str] = _default_token_factory,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: Zero-arg callable that returns a new
                SQLAlchemy ``Session`` (typically a ``sessionmaker``).
                Used as a context manager per call so the service holds
                no long-lived DB state.
            settings: Optional injected settings; falls back to the
                cached ``get_settings()`` so production code doesn't
                have to pass this explicitly.
            clock: Zero-arg callable returning a timezone-aware
                ``datetime``. Injected for tests; defaults to
                ``datetime.now(timezone.utc)``.
            token_factory: Zero-arg callable producing a fresh session
                token string. Injected for tests; defaults to
                ``secrets.token_urlsafe(32)``.
        """
        self._session_factory = session_factory
        self._settings = settings if settings is not None else get_settings()
        self._clock = clock
        self._token_factory = token_factory

    # ------------------------------------------------------------------
    # register
    # ------------------------------------------------------------------

    def register(self, nickname: str) -> RegistrationResult:
        """Register a new player.

        Steps:

        1. Run :func:`validate_nickname`. On :class:`LengthError` or
           :class:`CharsetError` return :class:`NicknameValidationError`
           without opening a DB session.
        2. Open a session. Look up any existing Player whose
           ``nickname_ci`` matches AND whose ``session_expires_at > now``
           (an Active_Player). If one exists, return
           :class:`NicknameTaken`.
        3. Otherwise, insert a fresh row with a new UUID id, the
           validator's ``nickname`` and ``nickname_ci``, ``created_at``
           and ``session_expires_at`` derived from the injected clock,
           and a fresh ``session_token``.
        4. If the insert raises ``IntegrityError`` on the unique
           ``nickname_ci`` constraint, the collision is with an expired
           row that was not caught by step 2 (either a purge lag or a
           concurrent registration race). Roll back, look up the
           colliding row, and:
             * if it's still an Active_Player (concurrent registration
               won), return :class:`NicknameTaken`;
             * if it's expired, ``DELETE`` it (expired players are gone
               per the lounge-session framing — no need to preserve
               them) and retry the insert exactly once using the *same*
               freshly-generated id and token (neither was ever
               persisted on the failed attempt, so reusing them is
               safe and keeps the token_factory call count predictable:
               exactly one call per call to :meth:`register`).
           A second failure surfaces as :class:`NicknameTaken` so the
           caller observes a stable conflict rather than a 500.

        Args:
            nickname: Raw nickname as submitted by the client. Not
                stripped — the API layer decides whether to pre-strip.

        Returns:
            A :data:`RegistrationResult` variant.
        """
        validation = validate_nickname(nickname)
        if isinstance(validation, LengthError):
            return NicknameValidationError(
                code="length",
                details={
                    "min": validation.min_length,
                    "max": validation.max_length,
                    "actual": validation.length,
                },
            )
        if isinstance(validation, CharsetError):
            return NicknameValidationError(
                code="charset",
                details={"invalid_chars": list(validation.invalid_chars)},
            )
        assert isinstance(validation, Ok)  # exhaustive on ValidationResult

        now = self._clock()
        ttl = timedelta(seconds=self._settings.session_ttl_seconds)
        expires_at = now + ttl
        # Generate id + token ONCE per call. If the first insert races
        # against an expired row and we retry, we reuse these values so
        # observable side effects (token_factory invocations) happen at
        # most once per call to register.
        new_id = str(uuid.uuid4())
        new_token = self._token_factory()

        with self._session_factory() as session:
            # Step 2: active-player collision check.
            active = self._find_active_by_nickname_ci(
                session, validation.nickname_ci, now
            )
            if active is not None:
                return NicknameTaken(nickname_ci=validation.nickname_ci)

            # Step 3: insert. Built inline so we know exactly which
            # columns we are touching (Requirement 16.2 allowlist).
            new_row = self._build_player_row(
                validation=validation,
                new_id=new_id,
                new_token=new_token,
                now=now,
                expires_at=expires_at,
            )
            try:
                session.add(new_row)
                session.commit()
            except IntegrityError:
                session.rollback()
                retry_result = self._handle_collision_and_retry(
                    session=session,
                    validation=validation,
                    new_id=new_id,
                    new_token=new_token,
                    now=now,
                    expires_at=expires_at,
                )
                if retry_result is not None:
                    return retry_result
                # Retry inserted the row successfully; fall through to
                # return a success using the values we just persisted.
                # ``new_row`` is stale after rollback, so re-fetch.
                inserted = self._find_by_nickname_ci(
                    session, validation.nickname_ci
                )
                assert inserted is not None  # invariant: retry succeeded
                return RegistrationSuccess(
                    player_id=inserted.id,
                    session_token=inserted.session_token,
                    nickname=inserted.nickname,
                    created_at=_as_utc(inserted.created_at),
                    session_expires_at=_as_utc(inserted.session_expires_at),
                )

            return RegistrationSuccess(
                player_id=new_row.id,
                session_token=new_row.session_token,
                nickname=new_row.nickname,
                created_at=new_row.created_at,
                session_expires_at=new_row.session_expires_at,
            )

    # ------------------------------------------------------------------
    # authorize
    # ------------------------------------------------------------------

    def authorize(self, token: str | None) -> AuthorizationResult:
        """Authorize a request's Session_Token against a persisted Player.

        Steps:

        1. **Missing check (short-circuit).** ``None`` and empty or
           whitespace-only strings are classified as ``missing`` without
           opening a DB session. Session tokens are produced by
           :func:`secrets.token_urlsafe`, which never emits whitespace,
           so a whitespace payload cannot match any row — we treat it
           as an absent header rather than paying for a round-trip that
           would just miss. The original string is stripped for the
           missing detection; the stripped form is what we'd query, but
           since the empty/whitespace branch returns early no DB query
           actually happens here.
        2. **Unknown check.** Open a session and look up the Player row
           whose ``session_token`` equals ``token`` exactly (tokens are
           opaque — no case-folding, no trimming beyond the initial
           missing check). No match → ``unknown``.
        3. **Expired check.** Compare ``session_expires_at`` to the
           injected clock, normalized via :func:`_as_utc` so SQLite's
           naive round-tripped datetimes compare correctly. Strict
           inequality: ``session_expires_at <= now`` means expired
           (Requirement 7.5 — a bounded lifetime; a token *at* its
           expiry instant is no longer valid).
        4. **Success.** Return :class:`AuthorizedPlayer`. We do NOT
           mutate the row: no last-seen update, no sliding expiry. The
           session's bound is fixed at issuance time (Requirement 7.5).

        Args:
            token: Raw token value from the request. Accepting
                ``str | None`` lets the API layer forward the result of
                e.g. ``request.headers.get("Authorization")`` without
                first unwrapping.

        Returns:
            An :data:`AuthorizationResult` variant. :class:`Unauthorized`
            carries a structured ``reason`` for internal logging; the
            HTTP 401 response body stays generic per Requirement 7.3.
        """
        if token is None:
            return Unauthorized(reason="missing")
        stripped = token.strip()
        if not stripped:
            return Unauthorized(reason="missing")

        with self._session_factory() as session:
            stmt = select(Player).where(Player.session_token == stripped)
            row = session.execute(stmt).scalar_one_or_none()

            if row is None:
                return Unauthorized(reason="unknown")

            expires_at = _as_utc(row.session_expires_at)
            now = _as_utc(self._clock())
            if expires_at <= now:
                return Unauthorized(reason="expired")

            return AuthorizedPlayer(
                player_id=row.id,
                nickname=row.nickname,
                session_expires_at=expires_at,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_player_row(
        self,
        *,
        validation: Ok,
        new_id: str,
        new_token: str,
        now: datetime,
        expires_at: datetime,
    ) -> Player:
        """Construct a Player ORM row populating only allowlisted columns.

        Keeping this in one place makes Requirement 16.2 auditable: the
        column set touched here must stay in lockstep with the table's
        columns (enforced by ``test_minimal_columns_persisted``).

        The id and token are passed in rather than generated here so
        that a retry after an expired-row collision reuses the same
        identity. See :meth:`register` for why that matters.
        """
        return Player(
            id=new_id,
            nickname=validation.nickname,
            nickname_ci=validation.nickname_ci,
            created_at=now,
            session_token=new_token,
            session_expires_at=expires_at,
        )

    @staticmethod
    def _find_active_by_nickname_ci(
        session: Session, nickname_ci: str, now: datetime
    ) -> Player | None:
        """Return the Active_Player (session not expired) owning this key, if any."""
        stmt = select(Player).where(
            Player.nickname_ci == nickname_ci,
            Player.session_expires_at > now,
        )
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _find_by_nickname_ci(
        session: Session, nickname_ci: str
    ) -> Player | None:
        """Return whoever currently owns this ``nickname_ci`` key, active or not."""
        stmt = select(Player).where(Player.nickname_ci == nickname_ci)
        return session.execute(stmt).scalar_one_or_none()

    def _handle_collision_and_retry(
        self,
        *,
        session: Session,
        validation: Ok,
        new_id: str,
        new_token: str,
        now: datetime,
        expires_at: datetime,
    ) -> RegistrationResult | None:
        """Resolve a nickname_ci collision that escaped the active-check.

        Returns:
            * :class:`NicknameTaken` when the colliding row is still an
              Active_Player (concurrent registration won the race, or
              the expired-row detection was wrong).
            * A :class:`RegistrationSuccess` or :class:`NicknameTaken`
              as the outcome of exactly one retry after deleting an
              expired colliding row.
            * ``None`` to signal to the caller that the retry succeeded
              and it should build a success response from the freshly
              inserted row.
        """
        colliding = self._find_by_nickname_ci(session, validation.nickname_ci)
        if colliding is None:
            # Vanishingly unlikely — the unique constraint fired but the
            # row is gone by the time we look. Treat as a generic
            # conflict to stay on the safe side rather than looping.
            return NicknameTaken(nickname_ci=validation.nickname_ci)

        if _as_utc(colliding.session_expires_at) > now:
            # A concurrent registration won this nickname while we were
            # inserting. That's a legitimate active collision.
            return NicknameTaken(nickname_ci=validation.nickname_ci)

        # Colliding row is expired: purge it and retry exactly once.
        session.delete(colliding)
        session.commit()

        retry_row = self._build_player_row(
            validation=validation,
            new_id=new_id,
            new_token=new_token,
            now=now,
            expires_at=expires_at,
        )
        try:
            session.add(retry_row)
            session.commit()
        except IntegrityError:
            session.rollback()
            # A second collision means another concurrent registration
            # slid in between our delete and our retry insert. Surface
            # a stable conflict instead of a 500.
            return NicknameTaken(nickname_ci=validation.nickname_ci)

        # Retry succeeded. Signal to the caller via None.
        return None


__all__ = [
    "AuthorizationResult",
    "AuthorizedPlayer",
    "NicknameTaken",
    "NicknameValidationError",
    "PlayerService",
    "RegistrationResult",
    "RegistrationSuccess",
    "Unauthorized",
]
