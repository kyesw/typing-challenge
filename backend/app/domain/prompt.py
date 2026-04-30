"""Pure prompt validator for the Prompt_Repository.

Implements the format-level rules for a typing Prompt:

- ``text`` is non-empty and its length is in the closed interval
  ``[MIN_TEXT_LENGTH, MAX_TEXT_LENGTH]`` = ``[100, 500]`` (Requirements
  11.2 / 11.3).
- ``difficulty`` is either absent (``None``) or exactly one of the
  fixed values ``{"easy", "medium", "hard"}`` (Requirement 11.4). The
  string values are case-sensitive and match the lowercase form used
  by :class:`app.persistence.models.PromptDifficulty`.
- ``language`` is a non-empty, non-whitespace string. A full BCP-47
  check is intentionally out of scope for this task; the seed data
  uses ``"en"``.

The function is pure and deterministic: no logging, no I/O, no
module-level mutable state. It does not depend on FastAPI or
SQLAlchemy so it can be exercised directly by unit tests, Hypothesis
property tests (task 2.5), and the persistence-layer seed loader
(:mod:`app.persistence.prompt_seed`).

Validation order is ``text → difficulty → language`` so that a more
specific error always surfaces when an entry violates multiple rules,
mirroring the ordering style of
:func:`app.domain.nickname.validate_nickname`.

Requirements addressed:
- 11.2 (Prompt identity + language + text non-empty)
- 11.3 (Prompt text length ``[100, 500]``)
- 11.4 (Difficulty constrained when present)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Minimum allowed prompt text length (inclusive). Requirement 11.3.
MIN_TEXT_LENGTH: int = 100

#: Maximum allowed prompt text length (inclusive). Requirement 11.3.
MAX_TEXT_LENGTH: int = 500

#: Allowed difficulty values (Requirement 11.4). Case-sensitive. The
#: order matches the ``PromptDifficulty`` enum in the persistence layer
#: but is not relied upon for validation.
ALLOWED_DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OkPrompt:
    """Successful validation result.

    Attributes:
        text: The prompt passage, unchanged.
        difficulty: One of the values in :data:`ALLOWED_DIFFICULTIES`,
            or ``None`` when no difficulty was supplied.
        language: The submitted language code, unchanged.
    """

    text: str
    difficulty: str | None
    language: str


@dataclass(frozen=True)
class TextError:
    """Validation failure: ``text`` is empty or out of range.

    Requirements 11.2 / 11.3.

    Attributes:
        reason: Which rule was violated.
        length: The observed ``len(text)`` of the input.
        min_length: Echoes :data:`MIN_TEXT_LENGTH` for call sites that
            want to format a "got N, expected at least M" message.
        max_length: Echoes :data:`MAX_TEXT_LENGTH`.
    """

    reason: Literal["empty", "too_short", "too_long"]
    length: int
    min_length: int = MIN_TEXT_LENGTH
    max_length: int = MAX_TEXT_LENGTH


@dataclass(frozen=True)
class DifficultyError:
    """Validation failure: ``difficulty`` is not in the allowed set.

    Requirement 11.4.

    Attributes:
        value: The supplied difficulty string, unchanged.
        allowed: The full set of permitted values.
    """

    value: str
    allowed: tuple[str, ...] = ALLOWED_DIFFICULTIES


@dataclass(frozen=True)
class LanguageError:
    """Validation failure: ``language`` is empty or only whitespace.

    Requirement 11.2.
    """

    value: str


PromptValidationResult = OkPrompt | TextError | DifficultyError | LanguageError


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_prompt(
    *,
    text: str,
    difficulty: str | None,
    language: str,
) -> PromptValidationResult:
    """Validate the format of a submitted prompt.

    Args:
        text: The passage to type. Must be a :class:`str`.
        difficulty: Either ``None`` or a :class:`str`. When provided,
            must be one of :data:`ALLOWED_DIFFICULTIES`.
        language: The language code. Must be a non-empty,
            non-whitespace :class:`str`.

    Returns:
        * :class:`OkPrompt` with the inputs echoed back when all three
          rules are satisfied.
        * :class:`TextError` when ``text`` is empty or its length is
          outside ``[MIN_TEXT_LENGTH, MAX_TEXT_LENGTH]``.
        * :class:`DifficultyError` when ``difficulty`` is a non-``None``
          string outside :data:`ALLOWED_DIFFICULTIES`.
        * :class:`LanguageError` when ``language`` is empty or only
          whitespace.

    Raises:
        TypeError: If ``text`` is not ``str``, if ``language`` is not
            ``str``, or if ``difficulty`` is neither ``str`` nor
            ``None``. Callers are expected to pass typed inputs.

    The validator checks ``text`` first, then ``difficulty``, then
    ``language``. Callers that violate more than one rule receive the
    first-in-order failure; this lets the seed loader report the most
    specific reason per entry (see
    :func:`app.persistence.prompt_seed.load_seed_prompts`).
    """
    # --- Type guards ------------------------------------------------------
    if not isinstance(text, str):
        raise TypeError(
            f"prompt text must be str, got {type(text).__name__}"
        )
    if difficulty is not None and not isinstance(difficulty, str):
        raise TypeError(
            f"prompt difficulty must be str or None, got {type(difficulty).__name__}"
        )
    if not isinstance(language, str):
        raise TypeError(
            f"prompt language must be str, got {type(language).__name__}"
        )

    # --- Text check (Requirement 11.2 / 11.3) -----------------------------
    length = len(text)
    if length == 0:
        return TextError(reason="empty", length=0)
    if length < MIN_TEXT_LENGTH:
        return TextError(reason="too_short", length=length)
    if length > MAX_TEXT_LENGTH:
        return TextError(reason="too_long", length=length)

    # --- Difficulty check (Requirement 11.4) ------------------------------
    if difficulty is not None and difficulty not in ALLOWED_DIFFICULTIES:
        return DifficultyError(value=difficulty)

    # --- Language check (Requirement 11.2) --------------------------------
    if len(language) == 0 or language.strip() == "":
        return LanguageError(value=language)

    return OkPrompt(text=text, difficulty=difficulty, language=language)


__all__ = [
    "ALLOWED_DIFFICULTIES",
    "MAX_TEXT_LENGTH",
    "MIN_TEXT_LENGTH",
    "DifficultyError",
    "LanguageError",
    "OkPrompt",
    "PromptValidationResult",
    "TextError",
    "validate_prompt",
]
