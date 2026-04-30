"""Pure game-state machine helpers for the Game_Service.

The Typing Game's Game_Service owns the lifecycle of a single typing
attempt. This module encodes *only* the state-machine decision logic
as a pure function so the rule is exercised in isolation by unit
tests and the Hypothesis property test in task 4.6. The function is
deterministic, has no I/O, and does not depend on SQLAlchemy or
FastAPI. The service layer (tasks 4.2–4.5) is responsible for
persisting the new status, updating timestamps, and broadcasting
lifecycle events on the Realtime_Channel.

State machine (Requirements 8.1–8.5)::

    pending --BeginTyping--> in_progress
    in_progress --Complete--> completed
    pending --Abandon--> abandoned
    in_progress --Abandon--> abandoned

Any other ``(status, event)`` pair is rejected and leaves the status
unchanged (Requirement 8.5).

Return style
------------

The :func:`transition` function uses a result-like return value
consistent with the rest of the domain package (see
:mod:`app.domain.nickname` and :mod:`app.domain.prompt`):

- :class:`TransitionOk` carries the ``new_status`` produced by an
  allowed transition.
- :class:`InvalidTransition` carries the rejected ``current`` status
  and ``event`` plus a free-form ``reason`` string. The sentinel is
  returned, not raised, so callers can pattern-match on the result in
  the same style as the other domain modules.

The allowed-transition table is the single source of truth and is
exported as :data:`ALLOWED_TRANSITIONS` so tests (4.6) can iterate
over it without re-spelling the rules.

Requirements addressed:
- 8.1 (Initial status ``pending``)
- 8.2 (``pending → in_progress`` on begin-typing)
- 8.3 (``in_progress → completed`` on score persisted)
- 8.4 (``pending | in_progress → abandoned`` on abandon or timeout)
- 8.5 (Any other transition is rejected, status unchanged)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

# ---------------------------------------------------------------------------
# Status and event enums
# ---------------------------------------------------------------------------


class GameStatus(str, Enum):
    """The four legal statuses of a Game.

    The values are the lowercase strings used by the persistence layer
    (``games.status``) and by the API responses, so ``GameStatus`` can
    be read and written without a separate mapping table.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class GameEvent(str, Enum):
    """Lifecycle events that drive :func:`transition`.

    - ``BEGIN_TYPING``: the countdown ended and the typing phase is
      starting. Requirement 8.2.
    - ``COMPLETE``: a Score has been persisted for the in-progress
      Game. Requirement 8.3.
    - ``ABANDON``: the Game is being abandoned, either explicitly by
      the player or because the server detected a timeout.
      Requirement 8.4.
    """

    BEGIN_TYPING = "begin_typing"
    COMPLETE = "complete"
    ABANDON = "abandon"


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Initial status for every freshly created Game. Requirement 8.1.
INITIAL_STATUS: GameStatus = GameStatus.PENDING

#: The full set of allowed ``(current_status, event) -> new_status``
#: transitions. Exposed as an immutable mapping so tests can iterate
#: over it without the risk of mutating the module-level table.
#:
#: Keeping the transitions in a single dictionary (rather than a nested
#: ``if`` ladder inside :func:`transition`) lets the property test in
#: task 4.6 enumerate allowed transitions directly and lets the unit
#: tests here round-trip every valid pair without duplication.
ALLOWED_TRANSITIONS: Mapping[tuple[GameStatus, GameEvent], GameStatus] = (
    MappingProxyType(
        {
            (GameStatus.PENDING, GameEvent.BEGIN_TYPING): GameStatus.IN_PROGRESS,
            (GameStatus.IN_PROGRESS, GameEvent.COMPLETE): GameStatus.COMPLETED,
            (GameStatus.PENDING, GameEvent.ABANDON): GameStatus.ABANDONED,
            (GameStatus.IN_PROGRESS, GameEvent.ABANDON): GameStatus.ABANDONED,
        }
    )
)

#: The set of statuses from which no further transition is possible.
#: Any event applied to one of these statuses yields
#: :class:`InvalidTransition`.
TERMINAL_STATUSES: frozenset[GameStatus] = frozenset(
    {GameStatus.COMPLETED, GameStatus.ABANDONED}
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionOk:
    """Successful transition result.

    Attributes:
        new_status: The status the Game should move to. The caller is
            responsible for persisting the change.
    """

    new_status: GameStatus


@dataclass(frozen=True)
class InvalidTransition:
    """Rejected transition result.

    The current status is left unchanged (Requirement 8.5); the caller
    must not apply any state mutation when it sees this value.

    Attributes:
        current: The status at the time the event was applied.
        event: The event that was attempted.
        reason: One of ``"terminal_status"`` (the Game is already in a
            terminal state) or ``"not_allowed"`` (the pair is simply
            not in :data:`ALLOWED_TRANSITIONS`).
    """

    current: GameStatus
    event: GameEvent
    reason: str


TransitionResult = TransitionOk | InvalidTransition


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition(current: GameStatus, event: GameEvent) -> TransitionResult:
    """Decide the Game's new status for ``(current, event)``.

    Args:
        current: The Game's status *before* the event is applied.
        event: The lifecycle event being applied.

    Returns:
        :class:`TransitionOk` with the new status when
        ``(current, event)`` is in :data:`ALLOWED_TRANSITIONS`; otherwise
        :class:`InvalidTransition` carrying the rejected inputs and a
        reason. The function never raises for business-rule violations;
        it only raises :class:`TypeError` when inputs are not of the
        expected enum types.

    Raises:
        TypeError: If ``current`` is not a :class:`GameStatus` or
            ``event`` is not a :class:`GameEvent`. Callers are expected
            to pass typed inputs; the helper does not coerce.
    """
    if not isinstance(current, GameStatus):
        raise TypeError(
            f"current must be GameStatus, got {type(current).__name__}"
        )
    if not isinstance(event, GameEvent):
        raise TypeError(
            f"event must be GameEvent, got {type(event).__name__}"
        )

    # Terminal statuses are reported with a distinct reason so the
    # service layer can tell "game is already over" apart from "this
    # event isn't defined for the current status".
    if current in TERMINAL_STATUSES:
        return InvalidTransition(
            current=current,
            event=event,
            reason="terminal_status",
        )

    new_status = ALLOWED_TRANSITIONS.get((current, event))
    if new_status is None:
        return InvalidTransition(
            current=current,
            event=event,
            reason="not_allowed",
        )
    return TransitionOk(new_status=new_status)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "GameEvent",
    "GameStatus",
    "INITIAL_STATUS",
    "InvalidTransition",
    "TERMINAL_STATUSES",
    "TransitionOk",
    "TransitionResult",
    "transition",
]
