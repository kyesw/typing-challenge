"""Unit tests for the pure prompt validator (task 2.4).

Covers the format-level rules implemented by
:func:`app.domain.prompt.validate_prompt`:

- Text length boundaries ``[100, 500]`` (Requirements 11.2 / 11.3).
- Empty / too-short / too-long all surface as :class:`TextError`
  carrying the correct ``reason`` and observed ``length``.
- Difficulty is constrained to ``{"easy", "medium", "hard"}`` when
  present; ``None`` is accepted (Requirement 11.4).
- Language must be a non-empty, non-whitespace string (Requirement
  11.2).
- Validation order: text → difficulty → language.
- Non-str inputs raise :class:`TypeError`.

The Hypothesis property test for universal prompt validity (task 2.5)
is intentionally NOT implemented here.
"""

from __future__ import annotations

import pytest

from app.domain.prompt import (
    ALLOWED_DIFFICULTIES,
    MAX_TEXT_LENGTH,
    MIN_TEXT_LENGTH,
    DifficultyError,
    LanguageError,
    OkPrompt,
    TextError,
    validate_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_of(length: int) -> str:
    """Return an ASCII text payload of exactly ``length`` characters."""
    return "a" * length


# ---------------------------------------------------------------------------
# Text length boundaries (Requirements 11.2 / 11.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [MIN_TEXT_LENGTH, MAX_TEXT_LENGTH, 200])
def test_accepts_text_at_boundaries_and_within_range(length: int) -> None:
    text = _text_of(length)
    result = validate_prompt(text=text, difficulty=None, language="en")
    assert isinstance(result, OkPrompt)
    assert result.text == text
    assert result.difficulty is None
    assert result.language == "en"


@pytest.mark.parametrize(
    "length, expected_reason",
    [
        (0, "empty"),
        (MIN_TEXT_LENGTH - 1, "too_short"),
        (MAX_TEXT_LENGTH + 1, "too_long"),
    ],
)
def test_rejects_text_outside_range_with_correct_reason_and_length(
    length: int, expected_reason: str
) -> None:
    text = _text_of(length)
    result = validate_prompt(text=text, difficulty=None, language="en")
    assert isinstance(result, TextError)
    assert result.reason == expected_reason
    assert result.length == length
    assert result.min_length == MIN_TEXT_LENGTH
    assert result.max_length == MAX_TEXT_LENGTH


# ---------------------------------------------------------------------------
# Difficulty allowed set (Requirement 11.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("difficulty", list(ALLOWED_DIFFICULTIES))
def test_accepts_each_allowed_difficulty(difficulty: str) -> None:
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH),
        difficulty=difficulty,
        language="en",
    )
    assert isinstance(result, OkPrompt)
    assert result.difficulty == difficulty


def test_accepts_missing_difficulty() -> None:
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH),
        difficulty=None,
        language="en",
    )
    assert isinstance(result, OkPrompt)
    assert result.difficulty is None


@pytest.mark.parametrize(
    "bad_difficulty",
    [
        "Easy",       # wrong casing
        "EASY",       # wrong casing
        "  easy  ",   # whitespace padding is not stripped
        "extreme",    # not in the allowed set
        "",           # empty string is not None
    ],
)
def test_rejects_disallowed_difficulty_with_supplied_value(
    bad_difficulty: str,
) -> None:
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH),
        difficulty=bad_difficulty,
        language="en",
    )
    assert isinstance(result, DifficultyError)
    assert result.value == bad_difficulty
    assert result.allowed == ALLOWED_DIFFICULTIES


# ---------------------------------------------------------------------------
# Language non-empty / non-whitespace (Requirement 11.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_language", ["", " ", "\t", "   \n  "])
def test_rejects_empty_or_whitespace_only_language(bad_language: str) -> None:
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH),
        difficulty=None,
        language=bad_language,
    )
    assert isinstance(result, LanguageError)
    assert result.value == bad_language


def test_accepts_typical_language_codes() -> None:
    for code in ("en", "en-US", "ko"):
        result = validate_prompt(
            text=_text_of(MIN_TEXT_LENGTH),
            difficulty=None,
            language=code,
        )
        assert isinstance(result, OkPrompt)
        assert result.language == code


# ---------------------------------------------------------------------------
# Validation order: text → difficulty → language
# ---------------------------------------------------------------------------


def test_text_error_takes_precedence_over_difficulty_error() -> None:
    # Text too short AND invalid difficulty. Text wins.
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH - 1),
        difficulty="extreme",
        language="en",
    )
    assert isinstance(result, TextError)
    assert result.reason == "too_short"


def test_text_error_takes_precedence_over_language_error() -> None:
    # Text empty AND empty language. Text wins.
    result = validate_prompt(text="", difficulty=None, language="")
    assert isinstance(result, TextError)
    assert result.reason == "empty"


def test_difficulty_error_takes_precedence_over_language_error() -> None:
    # Valid text, invalid difficulty, invalid language. Difficulty wins.
    result = validate_prompt(
        text=_text_of(MIN_TEXT_LENGTH),
        difficulty="extreme",
        language="",
    )
    assert isinstance(result, DifficultyError)
    assert result.value == "extreme"


# ---------------------------------------------------------------------------
# Non-str input → TypeError
# ---------------------------------------------------------------------------


def test_non_str_text_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_prompt(text=None, difficulty=None, language="en")  # type: ignore[arg-type]


def test_non_str_language_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_prompt(
            text=_text_of(MIN_TEXT_LENGTH),
            difficulty=None,
            language=123,  # type: ignore[arg-type]
        )


def test_non_str_non_none_difficulty_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_prompt(
            text=_text_of(MIN_TEXT_LENGTH),
            difficulty=0,  # type: ignore[arg-type]
            language="en",
        )
