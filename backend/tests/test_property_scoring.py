"""Property-based tests for pure scoring (tasks 5.3, 5.4, 5.5).

These tests exercise the pure functions exposed by
:mod:`app.domain.scoring`: no database, no clock, no service wiring.
Keeping them at the pure-function layer makes them fast and sharply
scoped to the invariants each property is supposed to verify.

Properties covered:

- **Property 4: WPM is non-negative.** Over arbitrary typed text,
  prompt text, and elapsed time (including zero and negative), the
  value returned by :func:`compute_wpm` is always ``>= 0``.
  **Validates: Requirement 4.1.**

- **Property 5: Accuracy is in ``[0, 100]``.** Over arbitrary typed
  text and prompt text (including empty strings), the value returned
  by :func:`compute_accuracy` is always in the closed interval
  ``[0.0, 100.0]``.
  **Validates: Requirement 4.2.**

- **Property 6: Points derivation is deterministic.** For any
  ``(wpm, accuracy)`` pair, repeated invocations of
  :func:`compute_points` produce identical outputs and an integer type.
  **Validates: Requirement 4.3.**

The service-layer echo of Property 7 (server-authoritative timing)
lives in ``test_property_scoring_service.py`` and the cross-game
Property 8 (exactly one Score per completed Game) lives in
``test_property_score_uniqueness.py``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument

from app.domain.scoring import (
    compute_accuracy,
    compute_points,
    compute_wpm,
)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "scoring-property",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    # Profile already registered in a sibling module or during re-import.
    pass

settings.load_profile("scoring-property")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# Text strategies are capped at 200 characters so the tests stay fast
# while still exploring the full space of per-position match/mismatch
# between ``typed`` and ``prompt``. The compute functions are O(n) in
# the min length of the two inputs, so the cap is about runtime, not
# coverage.
_text_strategy = st.text(max_size=200)

# Elapsed includes zero and negative values: Requirement 4.1 demands
# the result be non-negative in every case, including degenerate
# clocks. ``allow_nan=False`` / ``allow_infinity=False`` rule out IEEE
# exotica since the service layer never produces them (elapsed is
# always a finite ``timedelta.total_seconds()``).
_elapsed_strategy = st.floats(
    min_value=-1e6,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)

# WPM and accuracy inputs for the points determinism property. Bounds
# reflect the realistic output ranges of ``compute_wpm`` /
# ``compute_accuracy`` (WPM in practice is well under 1000; accuracy
# is always in [0, 100]). We deliberately do NOT generate negative
# values here — Requirement 4.3 is about determinism over
# well-formed inputs; the ``compute_points`` unit tests
# (:mod:`tests.test_domain_scoring`) cover the defensive clamp.
_wpm_strategy = st.floats(
    min_value=0.0,
    max_value=1000.0,
    allow_nan=False,
    allow_infinity=False,
)
_accuracy_strategy = st.floats(
    min_value=0.0,
    max_value=100.0,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property 4: WPM is non-negative
# ---------------------------------------------------------------------------


@given(
    typed=_text_strategy,
    prompt=_text_strategy,
    elapsed=_elapsed_strategy,
)
@settings(max_examples=200, deadline=None)
def test_compute_wpm_is_non_negative(
    typed: str, prompt: str, elapsed: float
) -> None:
    """Property 4: WPM is non-negative.

    For any typed text, any prompt text, and any elapsed time —
    including the degenerate zero / negative cases — the Scoring
    function guarantees ``wpm >= 0`` (Requirement 4.1).

    Validates: Requirement 4.1.
    """
    result = compute_wpm(typed, prompt, elapsed)
    assert result >= 0.0


# ---------------------------------------------------------------------------
# Property 5: Accuracy is in [0, 100]
# ---------------------------------------------------------------------------


@given(typed=_text_strategy, prompt=_text_strategy)
@settings(max_examples=200, deadline=None)
def test_compute_accuracy_is_in_bounds(typed: str, prompt: str) -> None:
    """Property 5: Accuracy is in the closed interval ``[0, 100]``.

    For any typed text and any prompt text (including empty strings),
    :func:`compute_accuracy` returns a value in ``[0.0, 100.0]``
    (Requirement 4.2).

    Validates: Requirement 4.2.
    """
    result = compute_accuracy(typed, prompt)
    assert 0.0 <= result <= 100.0


# ---------------------------------------------------------------------------
# Property 6: Points derivation is deterministic
# ---------------------------------------------------------------------------


@given(wpm=_wpm_strategy, accuracy=_accuracy_strategy)
@settings(max_examples=200, deadline=None)
def test_compute_points_is_deterministic(wpm: float, accuracy: float) -> None:
    """Property 6: Points derivation is deterministic.

    Repeated invocations of :func:`compute_points` on the same
    ``(wpm, accuracy)`` pair produce identical integer outputs
    (Requirement 4.3 / design Property 6).

    Validates: Requirement 4.3.
    """
    first = compute_points(wpm, accuracy)

    # Points must be an ``int`` so it persists into the ``scores.points``
    # integer column without coercion surprises.
    assert isinstance(first, int)

    # Call several times to rule out any hidden state or nondeterminism.
    for _ in range(5):
        again = compute_points(wpm, accuracy)
        assert again == first
        assert isinstance(again, int)
