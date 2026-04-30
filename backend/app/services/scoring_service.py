"""Scoring_Service — score computation and persistence (task 5.2).

Owns the write-path that turns a completed Game into a persisted
Score row. The *numeric* scoring logic is pure and lives in
:mod:`app.domain.scoring` (task 5.1); this service handles:

1. **Server-authoritative elapsed time.** The caller passes the
   typed text and the server clock for ``ended_at``; this service
   computes elapsed as ``ended_at - game.started_at``. The
   client-supplied elapsed value is never consulted
   (Requirements 3.6, 15.1, 15.2 / Property 7).
2. **Score persistence.** Writes exactly one Score row per Game.
   The ``UNIQUE`` constraint on ``scores.game_id`` (Requirement 4.4 /
   Property 8) is the backstop; this service additionally checks up
   front and returns :class:`ScoreAlreadyExists` on a double-submit
   rather than surfacing an ``IntegrityError``.
3. **Atomic state transition.** Uses the pure state machine
   (:mod:`app.domain.game_state`) to validate the
   ``in_progress → completed`` transition, then updates the Game row
   in the same session the Score was written into. The caller
   (:class:`GameService.complete`) owns the commit, so the Score
   insert and the status update land in a single transaction — if
   either fails, neither is persisted (Requirement 4.5).

Design notes:

* **Session-in, not session-factory-in.** :meth:`compute_and_persist`
  takes the *caller's* session so the Score insert and the Game
  status update share a transaction with the caller's work (e.g.,
  :class:`GameService.complete` may have its own reads on the same
  session). That's the opposite of :class:`PlayerService` /
  :class:`GameService.create_game`, which open a fresh session per
  call — those don't need to cooperate transactionally with anyone
  else.
* **No commit here.** The caller commits. The service flushes so the
  UNIQUE check lands inside the transaction, but does not commit.
* **Id + clock injection.** The Score id uses the injected
  ``id_factory`` (default ``uuid.uuid4``). There is no separate
  clock here: ``ended_at`` is supplied by the caller so it is the
  same timestamp the caller stamps on the Game row.

Requirements addressed:
- 3.6, 15.1, 15.2 (server-authoritative timing)
- 4.4, 4.5       (exactly one Score per Game; status transition)
- 8.3, 8.7       (``in_progress → completed``; ``ended_at > started_at``)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.game_state import (
    GameEvent,
    GameStatus as DomainGameStatus,
    TransitionOk,
    transition,
)
from ..domain.scoring import compute_accuracy, compute_points, compute_wpm
from ..persistence.models import Game, GameStatus, Prompt, Score


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordScoreSuccess:
    """A Score was computed, persisted, and the Game transitioned.

    Carries the values the caller needs to build the API response
    payload (task 8.4). The embedded ``player_id`` is the Game's
    owner, included so the caller can avoid re-reading the Game row.

    Attributes:
        game_id: The Game's id.
        score_id: The freshly-inserted Score row's id.
        player_id: The owning player's id (denormalized from
            ``games.player_id`` at the time of the write).
        wpm: Words-per-minute computed by :func:`compute_wpm`.
        accuracy: Accuracy percentage in ``[0, 100]``.
        points: Deterministic points value derived from ``wpm`` and
            ``accuracy`` (Requirement 4.3 / Property 6).
        elapsed_seconds: The ``(ended_at - started_at).total_seconds()``
            used as the server-authoritative elapsed time. Returned
            for logging / debugging; the API response does not expose
            it.
        ended_at: The server clock written to ``games.ended_at``.
            Returned so the caller can echo it in its own success
            response without re-reading the row.
        status: Always :data:`GameStatus.COMPLETED` on success.
    """

    game_id: str
    score_id: str
    player_id: str
    wpm: float
    accuracy: float
    points: int
    elapsed_seconds: float
    ended_at: datetime
    status: GameStatus


@dataclass(frozen=True)
class ScoreAlreadyExists:
    """A Score was already persisted for this Game.

    Returned when :meth:`ScoringService.compute_and_persist` detects
    an existing Score row via a pre-insert ``SELECT``. The check is
    cheap under the ``ix_scores_player_id`` index and the row's
    ``UNIQUE(game_id)`` constraint, and returning a structured value
    (instead of propagating an ``IntegrityError``) lets the caller
    treat double-submits as an idempotent no-op rather than a 500.

    Attributes:
        game_id: The Game's id.
        score_id: The id of the pre-existing Score row.
    """

    game_id: str
    score_id: str


@dataclass(frozen=True)
class GameNotEligible:
    """The Game cannot be scored.

    Two situations are collapsed into this single result:

    * The Game's status is not ``in_progress`` (e.g., already
      ``completed``, ``abandoned``, or still ``pending``). The caller
      should have caught this before invoking the service; returning
      a result instead of raising keeps the path the same as other
      service-layer mismatches (see :class:`GameService`).
    * The Game's ``started_at`` is ``NULL``. Under normal operation
      ``begin_typing`` (task 4.3) sets it before the typing phase,
      but a corrupted row or an out-of-order call path could leave
      it unset. Scoring without a start timestamp would violate
      Requirement 15.1, so the service refuses.

    Attributes:
        game_id: The Game's id.
        current_status: The Game's status as observed at the check.
        reason: Machine-readable discriminator for internal logging;
            not exposed on the API.
    """

    game_id: str
    current_status: GameStatus
    reason: Literal["not_in_progress", "missing_started_at"]


RecordScoreResult = RecordScoreSuccess | ScoreAlreadyExists | GameNotEligible


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_id_factory() -> str:
    """Opaque UUIDv4 string id — matches the convention used elsewhere."""
    return str(uuid.uuid4())


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite's ``DateTime(timezone=True)`` column round-trips naive
    values; treat naive as UTC rather than raising on comparison.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ScoringService:
    """Computes and persists Scores for completed Games.

    The service is stateless and safe to share across FastAPI workers.
    It does NOT own a session factory: :meth:`compute_and_persist`
    operates on the caller's session so both parties' writes land in
    the same transaction.
    """

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] = _default_id_factory,
    ) -> None:
        """Initialize the service.

        Args:
            id_factory: Zero-arg callable producing a fresh Score id
                string. Injected for tests; defaults to
                ``str(uuid.uuid4())``.
        """
        self._id_factory = id_factory

    # ------------------------------------------------------------------
    # compute_and_persist
    # ------------------------------------------------------------------

    def compute_and_persist(
        self,
        session: Session,
        game: Game,
        typed_text: str,
        ended_at: datetime,
    ) -> RecordScoreResult:
        """Compute the Score and write it into ``session`` atomically.

        Steps:

        1. Verify ``game.status`` is ``in_progress`` and
           ``game.started_at`` is set. Otherwise return
           :class:`GameNotEligible`.
        2. Verify no Score already exists for ``game.id``. If one
           does, return :class:`ScoreAlreadyExists` without writing.
        3. Load the Prompt text via the same session (the Game's
           ``prompt_id`` FK guarantees the row exists).
        4. Compute elapsed = ``(ended_at - started_at).total_seconds()``.
           Validate ``ended_at > started_at`` (Requirement 8.7); if
           not, return :class:`GameNotEligible` with reason
           ``missing_started_at`` — the row is structurally invalid
           for scoring.
        5. Delegate to the pure :mod:`app.domain.scoring` helpers to
           get WPM, accuracy, and points from the server-measured
           elapsed.
        6. Insert the Score row (same session; flush so UNIQUE
           constraint violations would surface *now*, inside the
           caller's transaction).
        7. Apply the ``in_progress → completed`` transition via the
           pure state machine. Update the Game row's ``status`` and
           ``ended_at`` in the same session.
        8. Return :class:`RecordScoreSuccess`. The caller commits.

        The caller is responsible for committing the session (or
        rolling back on its own error).

        Args:
            session: The caller's SQLAlchemy session. The Game must
                already be loaded into this session (the caller holds
                a live reference in ``game``).
            game: The Game ORM row, loaded in ``session``. Its
                ``status`` must be ``in_progress`` and its
                ``started_at`` must be non-null.
            typed_text: The text the player submitted. Passed through
                to :func:`compute_wpm` and :func:`compute_accuracy`
                verbatim — length guards and sanitization happen at
                the API boundary (task 9.4).
            ended_at: The server clock to stamp on the Game and to
                use as the "now" value for elapsed. Passing this in
                rather than reading a clock inside the service lets
                :class:`GameService.complete` use *the same*
                timestamp for both its timeout check and the Score
                computation — no risk of a sub-millisecond drift
                flipping the decision.

        Returns:
            A :data:`RecordScoreResult` variant.
        """
        # Step 1: eligibility.
        if game.status is not GameStatus.IN_PROGRESS:
            return GameNotEligible(
                game_id=game.id,
                current_status=game.status,
                reason="not_in_progress",
            )
        if game.started_at is None:
            return GameNotEligible(
                game_id=game.id,
                current_status=game.status,
                reason="missing_started_at",
            )

        # Step 2: check for an existing Score for this game. Cheap
        # under the UNIQUE(game_id) constraint's implicit index.
        existing_id = session.execute(
            select(Score.id).where(Score.game_id == game.id)
        ).scalar_one_or_none()
        if existing_id is not None:
            return ScoreAlreadyExists(game_id=game.id, score_id=existing_id)

        # Step 3: fetch the prompt text. FK guarantees existence; a
        # missing row means the database was mutated out from under
        # us and is worth surfacing as an error rather than scoring
        # against an empty string.
        prompt = session.get(Prompt, game.prompt_id)
        if prompt is None:  # pragma: no cover - FK invariant
            raise RuntimeError(
                f"Game {game.id!r} references missing prompt {game.prompt_id!r}"
            )

        # Step 4: elapsed (server-authoritative). Normalize both
        # timestamps to UTC so SQLite's naive round-trip doesn't
        # break arithmetic.
        started_at = _as_utc(game.started_at)
        ended_at_utc = _as_utc(ended_at)
        if ended_at_utc <= started_at:
            # Requirement 8.7 demands ``ended_at > started_at``. A
            # caller that hands us a non-increasing pair has a bug;
            # surface it as GameNotEligible so the API layer can
            # return a structured error rather than a 500. We reuse
            # the ``missing_started_at`` reason string since the
            # defect is analogous (the Game cannot be scored on the
            # clocks we were given).
            return GameNotEligible(
                game_id=game.id,
                current_status=game.status,
                reason="missing_started_at",
            )
        elapsed_seconds = (ended_at_utc - started_at).total_seconds()

        # Step 5: pure scoring. The domain helpers carry the
        # Property 4 / 5 / 6 invariants — WPM >= 0, accuracy in
        # [0, 100], deterministic points — so we don't need to
        # re-check them here.
        wpm = compute_wpm(typed_text, prompt.text, elapsed_seconds)
        accuracy = compute_accuracy(typed_text, prompt.text)
        points = compute_points(wpm, accuracy)

        # Step 6: insert the Score row. We flush (not commit) so any
        # UNIQUE(game_id) violation lands inside the caller's
        # transaction, not at a later session boundary. Under normal
        # operation step 2 already ruled out a duplicate; the flush
        # is defense-in-depth against concurrent writers.
        score_id = self._id_factory()
        score_row = Score(
            id=score_id,
            game_id=game.id,
            player_id=game.player_id,
            wpm=wpm,
            accuracy=accuracy,
            points=points,
            # created_at uses the column's server_default so it's in
            # sync with the DB clock; we don't override it here.
        )
        session.add(score_row)
        session.flush()

        # Step 7: transition the Game. Using the pure state machine
        # keeps this in lockstep with Property 12 and with the other
        # write paths (begin_typing, the future abandon path). The
        # domain / persistence enums share string values, so we
        # translate by value.
        current_domain = DomainGameStatus(game.status.value)
        outcome = transition(current_domain, GameEvent.COMPLETE)
        # Under the step-1 guard we know current is IN_PROGRESS and
        # COMPLETE is allowed, so the transition must succeed. Assert
        # so a future change to the table fails loudly.
        assert isinstance(outcome, TransitionOk), (
            "in_progress + COMPLETE must yield TransitionOk"
        )
        new_status = GameStatus(outcome.new_status.value)
        game.status = new_status
        game.ended_at = ended_at_utc

        # Step 8: caller commits. Return the success payload.
        return RecordScoreSuccess(
            game_id=game.id,
            score_id=score_id,
            player_id=game.player_id,
            wpm=wpm,
            accuracy=accuracy,
            points=points,
            elapsed_seconds=elapsed_seconds,
            ended_at=ended_at_utc,
            status=new_status,
        )


__all__ = [
    "GameNotEligible",
    "RecordScoreResult",
    "RecordScoreSuccess",
    "ScoreAlreadyExists",
    "ScoringService",
]
