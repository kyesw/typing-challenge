"""Unit tests for the pure nickname validator (task 2.2).

Covers only the format-level rules implemented by
:func:`app.domain.nickname.validate_nickname`:

- Length bounds ``[2, 20]`` (Requirement 1.5).
- Charset ``[A-Za-z0-9 _-]`` (Requirement 1.6).
- Length check precedes charset check.
- ``nickname_ci`` equals ``s.casefold()`` on success.
- Non-str input raises :class:`TypeError`.

The case-insensitive uniqueness rule (Requirement 1.7) is enforced at
the service layer and intentionally not tested here. The Hypothesis
property test for universal charset/length behaviour lives in task 2.3.
"""

from __future__ import annotations

import pytest

from app.domain.nickname import (
    MAX_LENGTH,
    MIN_LENGTH,
    CharsetError,
    LengthError,
    Ok,
    validate_nickname,
)


# ---------------------------------------------------------------------------
# Length boundaries (Requirement 1.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nickname",
    [
        "ab",                       # minimum length, 2
        "a" * MAX_LENGTH,           # maximum length, 20
        "A1",                       # mixed letter/digit at min length
        "valid_nick-name_01",       # within range, mixed charset
    ],
)
def test_accepts_boundary_and_typical_lengths(nickname: str) -> None:
    result = validate_nickname(nickname)
    assert isinstance(result, Ok)
    assert result.nickname == nickname


@pytest.mark.parametrize(
    "nickname, expected_length",
    [
        ("", 0),
        ("a", 1),
        ("a" * (MAX_LENGTH + 1), MAX_LENGTH + 1),
    ],
)
def test_rejects_lengths_outside_range_with_length_error(
    nickname: str, expected_length: int
) -> None:
    result = validate_nickname(nickname)
    assert isinstance(result, LengthError)
    assert result.length == expected_length
    assert result.min_length == MIN_LENGTH
    assert result.max_length == MAX_LENGTH


# ---------------------------------------------------------------------------
# Charset coverage (Requirement 1.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nickname",
    [
        "ABCDEF",           # uppercase letters
        "abcdef",           # lowercase letters
        "012345",           # digits
        "  ab  ",           # interior + surrounding spaces (no stripping)
        "__ab__",           # underscores
        "--ab--",           # hyphens
        "Mix 01_-",         # all allowed categories combined
    ],
)
def test_accepts_each_allowed_character_category(nickname: str) -> None:
    result = validate_nickname(nickname)
    assert isinstance(result, Ok)
    assert result.nickname == nickname


def test_rejects_disallowed_chars_with_unique_ordered_tuple() -> None:
    # "." appears first, then "@", then "." repeats (should dedupe),
    # interspersed with allowed characters which must NOT be collected.
    result = validate_nickname("ab.c@d.e")
    assert isinstance(result, CharsetError)
    assert result.invalid_chars == (".", "@")


@pytest.mark.parametrize(
    "nickname, expected_first_invalid",
    [
        ("abc.", "."),
        ("ab@cd", "@"),
        ("ab\tcd", "\t"),
        ("ab\ncd", "\n"),
        ("caf\u00e9", "\u00e9"),   # accented latin letter 'é'
        ("hi\U0001F600", "\U0001F600"),  # emoji
    ],
)
def test_rejects_common_disallowed_characters(
    nickname: str, expected_first_invalid: str
) -> None:
    result = validate_nickname(nickname)
    assert isinstance(result, CharsetError)
    # The first invalid character in the returned tuple matches the
    # first offender in the input string.
    assert result.invalid_chars[0] == expected_first_invalid


# ---------------------------------------------------------------------------
# Rule ordering: length check precedes charset check
# ---------------------------------------------------------------------------


def test_length_check_precedes_charset_check() -> None:
    # Single disallowed character — charset would reject, but the length
    # rule fires first and surfaces a LengthError instead.
    result = validate_nickname("@")
    assert isinstance(result, LengthError)
    assert result.length == 1


# ---------------------------------------------------------------------------
# Case-folded companion output
# ---------------------------------------------------------------------------


def test_nickname_ci_is_casefold_of_input() -> None:
    result = validate_nickname("Alice")
    assert isinstance(result, Ok)
    assert result.nickname == "Alice"
    assert result.nickname_ci == "alice"


def test_nickname_ci_uses_casefold_not_lower() -> None:
    # ``casefold`` and ``lower`` agree on ASCII, which is the full
    # validator input space; this test just pins down the contract so
    # future changes don't silently swap the two.
    result = validate_nickname("MixedCASE_01")
    assert isinstance(result, Ok)
    assert result.nickname_ci == "MixedCASE_01".casefold()


# ---------------------------------------------------------------------------
# Non-str input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_input", [None, 123, 12.0, b"abcd", ["a", "b"], object()])
def test_non_str_input_raises_type_error(bad_input: object) -> None:
    with pytest.raises(TypeError):
        validate_nickname(bad_input)  # type: ignore[arg-type]
