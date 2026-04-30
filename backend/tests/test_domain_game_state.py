"""Unit tests for the pure game-state machine (task 4.1).

Covers only the decision logic implemented by
:func:`app.domain.game_state.transition` and the constants it exports:

- Initial status is ``pending`` (Requirement 8.1).
- Each allowed transition yields the documented new status
  (Requirements 8.2, 8.3, 8.4).
- A representative invalid transition is rejected and reports the
  inputs unchanged (Requirement 8.5).
- Terminal statuses (``completed``, ``abandoned``) reject every event
  with a distinguishable reason (Requirement 8.5).
- Non-enum inputs raise :class:`TypeError`.

The exhaustive "for any event sequence, only allowed transitions
occur" property (Property 12) is covered by the Hypothesis test in
task 4.6.
"""

from __future__ import annotations

import pytest

from app.domain.game_state import (
    ALLOWED_TRANSITIONS,
    INITIAL_STATUS,
    TERMINAL_STATUSES,
    GameEvent,
    GameStatus,
    InvalidTransition,
    TransitionOk,
    transition,
)


# ---------------------------------------------------------------------------
# Initial status (Requirement 8.1)
# ---------------------------------------------------------------------------


def test_initial_status_is_pending() -> None:
    assert INITIAL_STATUS is GameStatus.PENDING


# ---------------------------------------------------------------------------
# Allowed transitions (Requirements 8.2 / 8.3 / 8.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current, event, expected_new",
    [
        # Requirement 8.2: pending → in_progress on BeginTyping
        (GameStatus.PENDING, GameEvent.BEGIN_TYPING, GameStatus.IN_PROGRESS),
        # Requirement 8.3: in_progress → completed on Complete
        (GameStatus.IN_PROGRESS, GameEvent.COMPLETE, GameStatus.COMPLETED),
        # Requirement 8.4: pending → abandoned on Abandon
        (GameStatus.PENDING, GameEvent.ABANDON, GameStatus.ABANDONED),
        # Requirement 8.4: in_progress → abandoned on Abandon (e.g. timeout)
        (GameStatus.IN_PROGRESS, GameEvent.ABANDON, GameStatus.ABANDONED),
    ],
)
def test_allowed_transitions_yield_expected_new_status(
    current: GameStatus, event: GameEvent, expected_new: GameStatus
) -> None:
    result = transition(current, event)
    assert isinstance(result, TransitionOk)
    assert result.new_status is expected_new


def test_allowed_transitions_table_matches_spec_exactly() -> None:
    # Lock the table down so accidental additions or removals surface
    # as a test failure rather than a silent behaviour change.
    assert dict(ALLOWED_TRANSITIONS) == {
        (GameStatus.PENDING, GameEvent.BEGIN_TYPING): GameStatus.IN_PROGRESS,
        (GameStatus.IN_PROGRESS, GameEvent.COMPLETE): GameStatus.COMPLETED,
        (GameStatus.PENDING, GameEvent.ABANDON): GameStatus.ABANDONED,
        (GameStatus.IN_PROGRESS, GameEvent.ABANDON): GameStatus.ABANDONED,
    }


# ---------------------------------------------------------------------------
# Rejected transitions (Requirement 8.5)
# ---------------------------------------------------------------------------


def test_representative_invalid_transition_is_rejected_not_allowed() -> None:
    # pending + Complete is not defined: you must BeginTyping first.
    result = transition(GameStatus.PENDING, GameEvent.COMPLETE)
    assert isinstance(result, InvalidTransition)
    assert result.current is GameStatus.PENDING
    assert result.event is GameEvent.COMPLETE
    assert result.reason == "not_allowed"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
@pytest.mark.parametrize("event", list(GameEvent))
def test_terminal_statuses_reject_every_event(
    terminal: GameStatus, event: GameEvent
) -> None:
    result = transition(terminal, event)
    assert isinstance(result, InvalidTransition)
    assert result.current is terminal
    assert result.event is event
    assert result.reason == "terminal_status"


# ---------------------------------------------------------------------------
# Type guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_current", ["pending", 0, None, object()])
def test_non_enum_current_raises_type_error(bad_current: object) -> None:
    with pytest.raises(TypeError):
        transition(bad_current, GameEvent.BEGIN_TYPING)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_event", ["begin_typing", 0, None, object()])
def test_non_enum_event_raises_type_error(bad_event: object) -> None:
    with pytest.raises(TypeError):
        transition(GameStatus.PENDING, bad_event)  # type: ignore[arg-type]
