"""Pure nickname validator for the Player_Service.

This module implements the format-level part of nickname validation
(length and character set). The case-insensitive uniqueness rule from
Requirement 1.7 is intentionally **out of scope** here because that
check depends on current Active_Player state and is owned by the
service layer (task 3.1) together with the ``players.nickname_ci UNIQUE``
constraint from task 2.1. The Hypothesis property test in task 2.3
composes the collision check on top of :func:`validate_nickname`.

The function is pure and deterministic: no logging, no I/O, no
module-level mutable state. It can therefore be called safely from
anywhere — the API boundary, the service layer, and Hypothesis tests.

Rules implemented here:

- Length must be in the closed interval ``[MIN_LENGTH, MAX_LENGTH]`` =
  ``[2, 20]`` (Requirement 1.5). Length is checked *before* the charset
  so that a one-character string containing a disallowed character
  surfaces as a :class:`LengthError`, not a :class:`CharsetError`.
- Allowed characters are exactly ``[A-Za-z0-9 _-]`` — ASCII letters,
  ASCII digits, space, underscore, hyphen (Requirement 1.6).
- The validator does **not** strip or otherwise mutate the input. The
  API layer decides whether to pre-strip (task 8.1 / 9.3).

Requirements addressed:
- 1.5 (Nickname length bounds)
- 1.6 (Nickname character set)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Minimum allowed nickname length (inclusive). Requirement 1.5.
MIN_LENGTH: int = 2

#: Maximum allowed nickname length (inclusive). Requirement 1.5.
MAX_LENGTH: int = 20

#: Compiled once at module load so ``validate_nickname`` allocates no
#: per-call regex state. Matches a string whose characters are *all* in
#: the allowed set defined by Requirement 1.6.
ALLOWED_CHARS_PATTERN: re.Pattern[str] = re.compile(r"\A[A-Za-z0-9 _\-]*\Z")

#: Same character class, but matching a single character at a time. Used
#: to collect the unique disallowed characters for :class:`CharsetError`.
_DISALLOWED_CHAR_PATTERN: re.Pattern[str] = re.compile(r"[^A-Za-z0-9 _\-]")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ok:
    """Successful validation result.

    Attributes:
        nickname: The submitted nickname, unchanged. The service layer
            persists this as ``players.nickname`` so the display casing
            is preserved (Requirement 1.3).
        nickname_ci: The case-folded form used for uniqueness lookups.
            The service layer writes this into
            ``players.nickname_ci`` (Requirement 1.7 / task 2.1).
    """

    nickname: str
    nickname_ci: str


@dataclass(frozen=True)
class LengthError:
    """Validation failure: length is outside ``[MIN_LENGTH, MAX_LENGTH]``.

    Requirement 1.5.
    """

    length: int
    min_length: int = MIN_LENGTH
    max_length: int = MAX_LENGTH


@dataclass(frozen=True)
class CharsetError:
    """Validation failure: at least one disallowed character is present.

    Requirement 1.6.

    Attributes:
        invalid_chars: Tuple of unique invalid characters in the order
            of first occurrence in the input.
    """

    invalid_chars: tuple[str, ...]


ValidationResult = Ok | LengthError | CharsetError


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_nickname(s: str) -> ValidationResult:
    """Validate the format of a submitted nickname.

    Args:
        s: The raw nickname string as submitted by the client. The
            validator does *not* strip whitespace; callers decide
            whether to pre-strip.

    Returns:
        * :class:`Ok` with ``nickname`` equal to ``s`` and
          ``nickname_ci`` equal to ``s.casefold()`` when both the length
          and charset rules are satisfied.
        * :class:`LengthError` when ``len(s)`` is outside
          ``[MIN_LENGTH, MAX_LENGTH]``. Checked before the charset so a
          one-character disallowed input surfaces as a length error.
        * :class:`CharsetError` when any character of ``s`` is not in
          the allowed set ``[A-Za-z0-9 _-]``. ``invalid_chars`` contains
          each unique offending character in order of first occurrence.

    Raises:
        TypeError: If ``s`` is not a :class:`str`. Callers are expected
            to pass a string; the validator does not coerce.
    """
    if not isinstance(s, str):
        raise TypeError(
            f"nickname must be str, got {type(s).__name__}"
        )

    # Length first (Requirement 1.5). Checking before charset ensures a
    # too-short string with a disallowed character reports as a length
    # error rather than a charset error.
    length = len(s)
    if length < MIN_LENGTH or length > MAX_LENGTH:
        return LengthError(length=length)

    # Charset check (Requirement 1.6).
    if not ALLOWED_CHARS_PATTERN.fullmatch(s):
        # Collect unique disallowed characters, preserving the order of
        # first occurrence. Using a dict for ordered-unique membership.
        seen: dict[str, None] = {}
        for match in _DISALLOWED_CHAR_PATTERN.finditer(s):
            ch = match.group(0)
            if ch not in seen:
                seen[ch] = None
        return CharsetError(invalid_chars=tuple(seen.keys()))

    return Ok(nickname=s, nickname_ci=s.casefold())


__all__ = [
    "ALLOWED_CHARS_PATTERN",
    "MIN_LENGTH",
    "MAX_LENGTH",
    "Ok",
    "LengthError",
    "CharsetError",
    "ValidationResult",
    "validate_nickname",
]
