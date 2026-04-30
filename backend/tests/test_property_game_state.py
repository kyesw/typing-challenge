"""Property-based test for the game-state machine (task 4.6).

**Property 12: Game state machine only allows defined transitions.**

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5.**

The Game_Service's lifecycle rule (see :mod:`app.domain.game_state`) is
encoded as a pure, total function :func:`transition`. This module drives
it with arbitrary sequences of :class:`GameEvent` values starting from
:data:`INITIAL_STATUS` (``pending``) and checks that the *entire*
observed status trajectory is consistent with the spec:

1. The trajectory starts at ``pending`` (Requirement 8.1).
2. Every consecutive ``(current, new)`` pair where the status actually
   changes is one of the four transitions in
   :data:`ALLOWED_TRANSITIONS` (Requirements 8.2, 8.3, 8.4).
3. Every status in the trajectory is one of the four defined
   :class:`GameStatus` values.
4. Once a terminal status is reached (``completed`` or ``abandoned``)
   the status never changes again for any subsequent event
   (Requirement 8.5, absorbing-state form).
5. When :func:`transition` returns :class:`InvalidTransition` the
   caller's current status is left unchanged (Requirement 8.5).

The ``@given`` form is preferred over ``RuleBasedStateMachine`` here
because the state machine is acyclic and trivially re-simulable: a
plain left-fold over the event list is the most direct expression of
"apply events in order, check each step".
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument

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
# Hypothesis profile.
# ---------------------------------------------------------------------------
#
# Matches the conventions used by ``test_property_nickname.py``:
# ``deadline=None`` for CI stability on a pure function, and
# ``print_blob=True`` so failing cases copy-paste into a reproducer.
# The registration is guarded because pytest can import a test module
# more than once during collection, which would otherwise raise
# ``InvalidArgument`` on the duplicate profile name.

try:
    settings.register_profile(
        "game-state-property",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    # Profile already registered (e.g., test module re-loaded). Safe to
    # ignore — the existing profile carries the same settings.
    pass

settings.load_profile("game-state-property")


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------
#
# ``st.sampled_from(list(GameEvent))`` covers every defined event with
# uniform weight; the list length range ([0, 30]) spans the empty
# trajectory (exercises the initial-status clause) through sequences
# long enough to reliably reach a terminal status and then be forced
# to stay there.

_events_strategy: st.SearchStrategy[list[GameEvent]] = st.lists(
    st.sampled_from(list(GameEvent)),
    min_size=0,
    max_size=30,
)


# ---------------------------------------------------------------------------
# Simulator.
# ---------------------------------------------------------------------------


def _simulate(events: list[GameEvent]) -> list[GameStatus]:
    """Drive :func:`transition` over ``events`` starting from pending.

    Returns the full status trajectory including the initial status.
    On :class:`InvalidTransition` the caller keeps the current status
    (Requirement 8.5), so the trajectory stays flat across rejected
    events — this is the same behaviour the service layer is required
    to produce when it sees an ``InvalidTransition`` result.
    """
    trajectory: list[GameStatus] = [INITIAL_STATUS]
    current = INITIAL_STATUS
    for event in events:
        result = transition(current, event)
        if isinstance(result, TransitionOk):
            current = result.new_status
        else:
            # InvalidTransition: current is left unchanged (8.5).
            assert isinstance(result, InvalidTransition)
            assert result.current is current
            assert result.event is event
        trajectory.append(current)
    return trajectory


# ---------------------------------------------------------------------------
# Property 12: state machine only allows defined transitions.
# ---------------------------------------------------------------------------


@given(events=_events_strategy)
@settings(max_examples=300, deadline=None)
def test_state_machine_only_allows_defined_transitions(
    events: list[GameEvent],
) -> None:
    """Property 12.

    Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5.
    """
    trajectory = _simulate(events)

    # --- 8.1: trajectory starts at pending ---------------------------------
    assert trajectory[0] is INITIAL_STATUS
    assert trajectory[0] is GameStatus.PENDING

    # --- Every observed status is one of the four defined values ----------
    # (Guards against any accidental sentinel leaking out of transition.)
    defined = set(GameStatus)
    for status in trajectory:
        assert status in defined

    # --- 8.2 / 8.3 / 8.4: every actual change is an allowed transition ----
    # Pairs where the status does not change correspond to
    # InvalidTransition outcomes; they are permitted and covered by 8.5.
    allowed_pairs = {(cur, new) for (cur, _event), new in ALLOWED_TRANSITIONS.items()}
    for prev, new in zip(trajectory, trajectory[1:]):
        if prev is new:
            continue
        assert (prev, new) in allowed_pairs, (
            f"unexpected transition {prev.value} -> {new.value}"
        )

    # --- 8.5 (absorbing form): terminal statuses never transition out -----
    # Once the trajectory enters a terminal status, the tail is constant.
    for idx, status in enumerate(trajectory):
        if status in TERMINAL_STATUSES:
            tail = trajectory[idx:]
            assert all(s is status for s in tail), (
                f"left terminal status {status.value} at index {idx}: "
                f"{[s.value for s in tail]}"
            )
            break


def _event_that_produced(prev: GameStatus, new: GameStatus) -> GameEvent:
    """Find the event whose ``(prev, event) -> new`` entry is in the table.

    Debugging helper for investigating counter-examples: given a
    *changed* pair, return an event that maps ``prev`` to ``new``.
    Not invoked by the properties themselves.
    """
    for (cur, event), result_new in ALLOWED_TRANSITIONS.items():
        if cur is prev and result_new is new:
            return event
    raise KeyError(f"no allowed event produces {prev.value} -> {new.value}")


# ---------------------------------------------------------------------------
# Directional sub-properties (clearer shrinks for each spec clause).
# ---------------------------------------------------------------------------


@given(events=_events_strategy)
@settings(max_examples=200, deadline=None)
def test_final_status_is_one_of_four_defined_values(
    events: list[GameEvent],
) -> None:
    """The final status is always one of the four GameStatus values.

    Validates: Requirements 8.1-8.5 (closure of the state space).
    """
    trajectory = _simulate(events)
    assert trajectory[-1] in set(GameStatus)


@given(events=_events_strategy)
@settings(max_examples=200, deadline=None)
def test_every_changed_pair_is_in_allowed_transitions(
    events: list[GameEvent],
) -> None:
    """Every (current, new) pair where the status changes is allowed.

    This restates the core of Property 12 in a form that Hypothesis
    can shrink directly to the first bad pair, without the terminal
    and initial-status clauses muddying the failure.

    Validates: Requirements 8.2, 8.3, 8.4, 8.5.
    """
    allowed_pairs = {(cur, new) for (cur, _event), new in ALLOWED_TRANSITIONS.items()}
    trajectory = _simulate(events)
    for prev, new in zip(trajectory, trajectory[1:]):
        if prev is new:
            continue
        assert (prev, new) in allowed_pairs, (
            f"illegal transition {prev.value} -> {new.value}"
        )


@given(events=_events_strategy)
@settings(max_examples=200, deadline=None)
def test_invalid_transitions_preserve_current_status(
    events: list[GameEvent],
) -> None:
    """InvalidTransition results do not mutate the caller's state.

    Walks the sequence explicitly (rather than via ``_simulate``) so the
    "current unchanged" invariant is asserted at the point the result
    is observed, not inferred from the trajectory.

    Validates: Requirement 8.5.
    """
    current = INITIAL_STATUS
    for event in events:
        before = current
        result = transition(current, event)
        if isinstance(result, InvalidTransition):
            # The caller keeps its state; current must not be advanced.
            assert result.current is before
            assert result.event is event
            # Do not mutate current.
            continue
        assert isinstance(result, TransitionOk)
        current = result.new_status


@given(events=_events_strategy)
@settings(max_examples=200, deadline=None)
def test_terminal_status_is_absorbing(events: list[GameEvent]) -> None:
    """Once in a terminal status, no further event changes the state.

    Validates: Requirement 8.5 (the ``status unchanged`` clause in its
    terminal form).
    """
    trajectory = _simulate(events)
    reached_terminal_at: int | None = None
    for idx, status in enumerate(trajectory):
        if status in TERMINAL_STATUSES:
            reached_terminal_at = idx
            break
    if reached_terminal_at is None:
        return  # No terminal status reached in this trajectory; nothing to check.
    terminal_status = trajectory[reached_terminal_at]
    for status in trajectory[reached_terminal_at:]:
        assert status is terminal_status
