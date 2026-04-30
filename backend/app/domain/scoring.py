"""Pure scoring functions for the Scoring_Service.

This module implements the numeric part of scoring a typing attempt:
words-per-minute (WPM), accuracy percentage, and the derived points
value. The functions are pure and deterministic — no I/O, no clocks,
no database access, no logging — so they can be exercised directly
by the property-based tests in tasks 5.3, 5.4, and 5.5 without any
service-layer wiring.

The service layer (:mod:`app.services.game_service` /
``ScoringService.record_score`` in task 5.2) is responsible for
persisting the resulting Score row, deriving the elapsed time from
the server-measured ``endedAt - startedAt`` (Requirements 3.6, 15.1,
15.2), and emitting realtime events.

Scoring model
-------------

Typing is compared character-by-character against the assigned prompt:
for every position ``i < min(len(typed), len(prompt))``, the character
is considered correct when ``typed[i] == prompt[i]``. Extra characters
beyond ``len(prompt)`` and missing characters before ``len(prompt)``
are simply "not correct" — they do not count toward ``correct_chars``.

- **WPM** (Requirement 4.1): the traditional "5 characters = 1 word"
  convention applied to correctly typed characters. Formally,
  ``wpm = (correct_chars / CHARS_PER_WORD) / (elapsed_seconds / 60)``.
  When ``elapsed_seconds <= 0`` the value is clamped to ``0.0`` so the
  "wpm >= 0" invariant from Requirement 4.1 holds in the degenerate
  case without raising.
- **Accuracy** (Requirement 4.2): ``correct_chars / len(prompt) * 100``
  clamped into ``[0, 100]``. When the prompt is empty the percentage
  is undefined; by convention this module returns ``100.0`` when both
  strings are empty (a vacuously "complete" submission) and ``0.0``
  when the prompt is empty but the player typed something (there is
  nothing correct to match against).
- **Points** (Requirement 4.3): deterministic derivation from WPM and
  accuracy: ``round(wpm * accuracy / 100 * POINTS_MULTIPLIER)`` clamped
  at ``0``. Using ``round`` makes the function pure in the usual
  Python sense — same inputs always map to the same integer output
  (Property 6 in the design document).

Requirements addressed:
- 4.1 (WPM is non-negative)
- 4.2 (Accuracy in ``[0, 100]``)
- 4.3 (Deterministic points derivation)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Traditional "word" length used by the WPM formula. The industry
#: convention for typing tests counts any five correctly typed
#: characters as one word; the Scoring_Service follows that convention
#: so reported WPM values are comparable to typical typing-test tools.
CHARS_PER_WORD: int = 5

#: Number of seconds in one minute. Named so the WPM formula reads as
#: ``chars / CHARS_PER_WORD / (elapsed_seconds / SECONDS_PER_MINUTE)``
#: rather than using a bare magic number.
SECONDS_PER_MINUTE: int = 60

#: Scaling factor for :func:`compute_points`. Keeping it as a module
#: constant makes it clear the value is a tunable coefficient, not a
#: magic number baked into the formula.
POINTS_MULTIPLIER: int = 10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_correct_chars(typed_text: str, prompt: str) -> int:
    """Count positions where ``typed_text`` and ``prompt`` agree.

    A character position ``i`` counts as correct when
    ``i < min(len(typed_text), len(prompt))`` and
    ``typed_text[i] == prompt[i]``. Characters in either string beyond
    the shared prefix length do not contribute.

    The helper is kept internal because the public scoring functions
    compose it differently (WPM divides by time, accuracy divides by
    prompt length) and exposing it could tempt callers to
    double-count.
    """
    # ``zip`` stops at the shorter of the two iterables, which is
    # exactly the ``min(len(typed), len(prompt))`` window we want.
    return sum(1 for t, p in zip(typed_text, prompt) if t == p)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_wpm(typed_text: str, prompt: str, elapsed_seconds: float) -> float:
    """Compute words-per-minute for a typing attempt.

    Uses the "5 characters = 1 word" convention on correctly typed
    characters (see :data:`CHARS_PER_WORD`).

    Args:
        typed_text: What the player actually submitted.
        prompt: The assigned prompt text to compare against.
        elapsed_seconds: Server-measured elapsed time in seconds.
            Values ``<= 0`` are treated as degenerate and force the
            result to ``0.0`` (Requirement 4.1).

    Returns:
        A non-negative ``float`` representing WPM. ``0.0`` when
        ``elapsed_seconds <= 0`` or when no characters match.

    Raises:
        TypeError: If ``typed_text`` or ``prompt`` is not a
            :class:`str`. Numeric coercion of ``elapsed_seconds`` is
            deferred to the arithmetic below, which will raise
            :class:`TypeError` for non-numeric inputs.
    """
    if not isinstance(typed_text, str):
        raise TypeError(
            f"typed_text must be str, got {type(typed_text).__name__}"
        )
    if not isinstance(prompt, str):
        raise TypeError(
            f"prompt must be str, got {type(prompt).__name__}"
        )

    # Clamp degenerate elapsed-times to 0 WPM. This keeps the
    # "wpm >= 0" invariant from Requirement 4.1 total: every input
    # produces a defined, non-negative result without raising.
    if elapsed_seconds <= 0:
        return 0.0

    correct_chars = _count_correct_chars(typed_text, prompt)
    words = correct_chars / CHARS_PER_WORD
    minutes = elapsed_seconds / SECONDS_PER_MINUTE
    wpm = words / minutes

    # Defensive clamp: ``correct_chars`` and ``minutes`` are both
    # non-negative here, so ``wpm`` is already ``>= 0``; the explicit
    # ``max`` makes the invariant visible at the return site.
    return max(0.0, wpm)


def compute_accuracy(typed_text: str, prompt: str) -> float:
    """Compute the accuracy percentage for a typing attempt.

    Args:
        typed_text: What the player actually submitted.
        prompt: The assigned prompt text to compare against.

    Returns:
        A ``float`` in the closed interval ``[0.0, 100.0]``
        (Requirement 4.2).

        The empty-prompt case is degenerate: percentage of matching
        characters is undefined when there is nothing to match
        against, so by convention this function returns:

        - ``100.0`` when both strings are empty — the player has
          correctly produced the entire (empty) prompt.
        - ``0.0`` when the prompt is empty but the player typed
          something — there is nothing correct to match against.

    Raises:
        TypeError: If ``typed_text`` or ``prompt`` is not a :class:`str`.
    """
    if not isinstance(typed_text, str):
        raise TypeError(
            f"typed_text must be str, got {type(typed_text).__name__}"
        )
    if not isinstance(prompt, str):
        raise TypeError(
            f"prompt must be str, got {type(prompt).__name__}"
        )

    if len(prompt) == 0:
        # Documented convention — see the docstring.
        return 100.0 if len(typed_text) == 0 else 0.0

    correct_chars = _count_correct_chars(typed_text, prompt)
    accuracy = correct_chars / len(prompt) * 100.0

    # Defensive clamp. The arithmetic above cannot exceed 100 nor drop
    # below 0 for the current ``_count_correct_chars`` definition, but
    # the explicit clamp keeps the public guarantee (Requirement 4.2)
    # robust to any future change in the counting helper.
    if accuracy < 0.0:
        return 0.0
    if accuracy > 100.0:
        return 100.0
    return accuracy


def compute_points(wpm: float, accuracy: float) -> int:
    """Derive the Score's ``points`` from ``wpm`` and ``accuracy``.

    The function is deterministic and pure: the same ``(wpm,
    accuracy)`` pair always produces the same integer result
    (Requirement 4.3 / Property 6).

    Formula::

        round(wpm * accuracy / 100 * POINTS_MULTIPLIER)

    The result is clamped at ``0`` so negative inputs (which should
    not arise from :func:`compute_wpm` or :func:`compute_accuracy`
    but could be fabricated by a caller) cannot produce a negative
    Score.

    Args:
        wpm: The attempt's words-per-minute. Expected ``>= 0``.
        accuracy: The attempt's accuracy percentage in ``[0, 100]``.

    Returns:
        A non-negative integer.
    """
    raw = wpm * accuracy / 100.0 * POINTS_MULTIPLIER
    # ``round`` uses banker's rounding in Python 3 but is still a pure
    # function of its argument, so Property 6 (determinism) holds.
    points = round(raw)
    return max(0, points)


__all__ = [
    "CHARS_PER_WORD",
    "POINTS_MULTIPLIER",
    "SECONDS_PER_MINUTE",
    "compute_accuracy",
    "compute_points",
    "compute_wpm",
]
