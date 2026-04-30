"""Unit tests for pure scoring functions (task 5.1).

Covers the decision logic implemented by
:func:`app.domain.scoring.compute_wpm`,
:func:`app.domain.scoring.compute_accuracy`, and
:func:`app.domain.scoring.compute_points`:

- Empty-prompt / empty-typed degenerate cases
  (Requirements 4.1, 4.2).
- Fully correct typing produces a positive WPM, 100% accuracy, and
  non-zero points (Requirements 4.1, 4.2, 4.3).
- Entirely wrong typing produces ``0`` correct chars, zero WPM,
  0% accuracy, and zero points (Requirements 4.1, 4.2, 4.3).
- Degenerate elapsed times (``0`` and negative) clamp WPM to ``0``
  without raising (Requirement 4.1).
- Accuracy is always in ``[0, 100]`` and WPM is always ``>= 0``
  (Requirements 4.1, 4.2) — including when the typed text is longer
  or shorter than the prompt.
- ``compute_points`` is deterministic over repeated calls
  (Requirement 4.3).

The exhaustive property-based tests for the WPM, accuracy, and
points bounds/determinism live in tasks 5.3, 5.4, and 5.5.
"""

from __future__ import annotations

import pytest

from app.domain.scoring import (
    CHARS_PER_WORD,
    POINTS_MULTIPLIER,
    SECONDS_PER_MINUTE,
    compute_accuracy,
    compute_points,
    compute_wpm,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_module_constants_have_expected_values() -> None:
    # Lock down the constants so an accidental tweak surfaces as a
    # test failure instead of a silent scoring shift.
    assert CHARS_PER_WORD == 5
    assert SECONDS_PER_MINUTE == 60
    assert POINTS_MULTIPLIER == 10


# ---------------------------------------------------------------------------
# compute_accuracy
# ---------------------------------------------------------------------------


def test_accuracy_empty_prompt_and_empty_typed_is_100() -> None:
    # Degenerate case documented on ``compute_accuracy``: both empty
    # means the player trivially produced the entire prompt.
    assert compute_accuracy("", "") == 100.0


def test_accuracy_empty_prompt_with_non_empty_typed_is_0() -> None:
    # Nothing to match against → no correct characters possible.
    assert compute_accuracy("hello", "") == 0.0


def test_accuracy_fully_correct_is_100() -> None:
    assert compute_accuracy("hello world", "hello world") == 100.0


def test_accuracy_all_wrong_is_0() -> None:
    # Every position disagrees → zero correct characters.
    assert compute_accuracy("bbbb", "aaaa") == 0.0


def test_accuracy_partial_match_is_proportional() -> None:
    # 3 of 4 prompt chars match → 75%.
    assert compute_accuracy("abcX", "abcd") == pytest.approx(75.0)


def test_accuracy_typed_longer_than_prompt_counts_only_prompt_length() -> None:
    # Extra characters past len(prompt) do not count as correct and
    # the denominator is len(prompt), so accuracy stays at 100%.
    assert compute_accuracy("hello world!!!", "hello world") == 100.0


def test_accuracy_typed_shorter_than_prompt_is_proportional() -> None:
    # Only 2 of 5 prompt positions matched.
    assert compute_accuracy("he", "hello") == pytest.approx(40.0)


def test_accuracy_bounds_are_respected_on_typical_inputs() -> None:
    # Sanity check on the documented invariant.
    for typed, prompt in [
        ("", "abc"),
        ("abc", "abc"),
        ("abcd", "abcd"),
        ("xyz", "abc"),
        ("aXcY", "abcd"),
    ]:
        result = compute_accuracy(typed, prompt)
        assert 0.0 <= result <= 100.0


def test_accuracy_rejects_non_string_typed() -> None:
    with pytest.raises(TypeError):
        compute_accuracy(123, "abc")  # type: ignore[arg-type]


def test_accuracy_rejects_non_string_prompt() -> None:
    with pytest.raises(TypeError):
        compute_accuracy("abc", 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_wpm
# ---------------------------------------------------------------------------


def test_wpm_zero_elapsed_is_zero() -> None:
    # Requirement 4.1 degenerate case: no time elapsed, no WPM.
    assert compute_wpm("hello", "hello", 0) == 0.0


def test_wpm_negative_elapsed_is_zero() -> None:
    # Clock skew or bad caller input must not yield negative WPM.
    assert compute_wpm("hello", "hello", -5) == 0.0


def test_wpm_all_wrong_is_zero_even_with_elapsed() -> None:
    # Zero correct characters → zero words → zero WPM regardless of
    # how long the player typed.
    assert compute_wpm("zzzzz", "aaaaa", 30) == 0.0


def test_wpm_fully_correct_60_seconds_matches_formula() -> None:
    # 10 correct chars / 5 chars-per-word / 1 minute = 2 wpm
    assert compute_wpm("abcdeabcde", "abcdeabcde", 60) == pytest.approx(2.0)


def test_wpm_fully_correct_30_seconds_doubles_the_rate() -> None:
    # Same 10 correct chars in half the time → 4 wpm.
    assert compute_wpm("abcdeabcde", "abcdeabcde", 30) == pytest.approx(4.0)


def test_wpm_empty_prompt_and_empty_typed_is_zero() -> None:
    # No correct characters to count when both strings are empty,
    # regardless of elapsed time.
    assert compute_wpm("", "", 60) == 0.0


def test_wpm_empty_prompt_with_typed_is_zero() -> None:
    # Nothing to match → no correct chars → 0 wpm.
    assert compute_wpm("hello", "", 60) == 0.0


def test_wpm_is_never_negative_for_sampled_inputs() -> None:
    for typed, prompt, elapsed in [
        ("", "", 0),
        ("", "abc", 1),
        ("abc", "abc", 1),
        ("XXX", "abc", 1),
        ("abcd", "abc", 1),
        ("ab", "abc", 0.5),
    ]:
        assert compute_wpm(typed, prompt, elapsed) >= 0.0


def test_wpm_rejects_non_string_typed() -> None:
    with pytest.raises(TypeError):
        compute_wpm(123, "abc", 1)  # type: ignore[arg-type]


def test_wpm_rejects_non_string_prompt() -> None:
    with pytest.raises(TypeError):
        compute_wpm("abc", 123, 1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_points
# ---------------------------------------------------------------------------


def test_points_zero_wpm_gives_zero_points() -> None:
    assert compute_points(0.0, 100.0) == 0


def test_points_zero_accuracy_gives_zero_points() -> None:
    assert compute_points(60.0, 0.0) == 0


def test_points_matches_documented_formula() -> None:
    # round(40 * 90 / 100 * 10) = round(360) = 360
    assert compute_points(40.0, 90.0) == 360


def test_points_is_deterministic_over_repeated_calls() -> None:
    # Requirement 4.3 / Property 6: identical inputs → identical output.
    samples = [
        (0.0, 0.0),
        (0.0, 100.0),
        (1.0, 50.0),
        (42.5, 87.25),
        (100.0, 100.0),
        (999.99, 99.99),
    ]
    for wpm, accuracy in samples:
        first = compute_points(wpm, accuracy)
        # Call several times to rule out any hidden state.
        for _ in range(5):
            assert compute_points(wpm, accuracy) == first


def test_points_clamps_negative_inputs_to_zero() -> None:
    # Defensive: negative wpm or accuracy shouldn't happen in practice
    # but the clamp must keep points non-negative.
    assert compute_points(-10.0, 50.0) == 0
    assert compute_points(50.0, -10.0) == 0
    assert compute_points(-10.0, -10.0) >= 0


def test_points_is_integer_type() -> None:
    result = compute_points(37.3, 82.6)
    assert isinstance(result, int)
