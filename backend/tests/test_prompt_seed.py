"""Tests for the prompt seed loader (task 2.4).

Covers the seed-JSON → DB pipeline introduced by
:mod:`app.persistence.prompt_seed`:

- :func:`load_seed_prompts` loads at least 20 entries from the
  packaged JSON and every returned :class:`OkPrompt` carries valid
  text length and difficulty/language values (Requirement 11).
- :func:`seed_prompts_if_empty` inserts the full set when the table
  is empty, returns the count, and is a no-op on subsequent calls
  (idempotency).
- An invalid entry in a temporary JSON file raises :class:`ValueError`
  that names the offending zero-based index.
- After seeding, raw SQL confirms every inserted row carries a
  ``difficulty`` value that is either NULL or drawn from
  ``{"easy", "medium", "hard"}`` — a belt-and-suspenders check
  against Requirement 11.4.
- The packaged JSON path resolves to an existing file inside the
  installed ``app.persistence`` package (packaging sanity).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.domain.prompt import (
    ALLOWED_DIFFICULTIES,
    MAX_TEXT_LENGTH,
    MIN_TEXT_LENGTH,
    OkPrompt,
)
from app.persistence import init_db
from app.persistence.prompt_seed import (
    DEFAULT_SEED_FILE,
    load_seed_prompts,
    seed_prompts_if_empty,
)
from app.persistence.models import PromptDifficulty


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_persistence.py so the DB behaves as in prod)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    """Fresh in-memory SQLite engine with the schema materialized."""
    eng = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(eng)
    return eng


# ---------------------------------------------------------------------------
# 1. Packaged JSON loads cleanly and every entry is valid
# ---------------------------------------------------------------------------


def test_load_seed_prompts_returns_at_least_twenty_valid_entries() -> None:
    prompts = load_seed_prompts()
    assert len(prompts) >= 20
    for index, prompt in enumerate(prompts):
        assert isinstance(prompt, OkPrompt), f"entry {index} is not OkPrompt"
        assert MIN_TEXT_LENGTH <= len(prompt.text) <= MAX_TEXT_LENGTH, (
            f"entry {index} text length {len(prompt.text)} out of range"
        )
        assert prompt.difficulty is None or prompt.difficulty in ALLOWED_DIFFICULTIES, (
            f"entry {index} difficulty {prompt.difficulty!r} not in allowed set"
        )
        assert isinstance(prompt.language, str) and prompt.language.strip() != "", (
            f"entry {index} language is empty"
        )


# ---------------------------------------------------------------------------
# 2. seed_prompts_if_empty is idempotent
# ---------------------------------------------------------------------------


def test_seed_prompts_if_empty_inserts_then_is_idempotent(engine: Engine) -> None:
    expected = len(load_seed_prompts())

    inserted = seed_prompts_if_empty(engine)
    assert inserted == expected

    with engine.connect() as conn:
        count_after_first = conn.execute(
            text("SELECT COUNT(*) FROM prompts")
        ).scalar_one()
    assert count_after_first == expected

    # Second call on the same engine: no writes, returns 0.
    second = seed_prompts_if_empty(engine)
    assert second == 0

    with engine.connect() as conn:
        count_after_second = conn.execute(
            text("SELECT COUNT(*) FROM prompts")
        ).scalar_one()
    assert count_after_second == expected


# ---------------------------------------------------------------------------
# 3. Invalid JSON entry → ValueError naming the index
# ---------------------------------------------------------------------------


def test_load_seed_prompts_rejects_invalid_entry_with_index_in_message(
    tmp_path: Path,
) -> None:
    # Entry index 1 has a 50-character text which is below MIN_TEXT_LENGTH.
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            [
                {
                    "text": "a" * MIN_TEXT_LENGTH,
                    "difficulty": "easy",
                    "language": "en",
                },
                {
                    "text": "a" * 50,  # too short — triggers TextError(too_short)
                    "difficulty": "easy",
                    "language": "en",
                },
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_seed_prompts(bad_file)

    message = str(excinfo.value)
    assert "index 1" in message
    # Ensures the reason gets surfaced so the operator can diagnose at a glance.
    assert "too_short" in message or "length" in message


def test_load_seed_prompts_rejects_non_list_root(tmp_path: Path) -> None:
    bad_file = tmp_path / "wrong_root.json"
    bad_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(ValueError):
        load_seed_prompts(bad_file)


def test_load_seed_prompts_rejects_non_dict_entry(tmp_path: Path) -> None:
    bad_file = tmp_path / "wrong_entry.json"
    bad_file.write_text(json.dumps(["not an object"]), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_seed_prompts(bad_file)
    assert "index 0" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. Round-trip: every inserted row has a valid difficulty value
# ---------------------------------------------------------------------------


def test_inserted_rows_only_carry_allowed_difficulty_values(engine: Engine) -> None:
    seed_prompts_if_empty(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT difficulty FROM prompts")).all()

    assert rows, "expected seed rows to be present after seeding"
    # ``PromptDifficulty`` in the ORM is stored via SQLAlchemy's
    # ``Enum`` column with ``native_enum=False``, which persists the
    # Python enum *name* rather than its ``value``. That means the
    # raw storage values are ``{"EASY", "MEDIUM", "HARD"}`` (uppercase)
    # while the validator surface uses lowercase. Each stored
    # uppercase key bijectively maps onto exactly one allowed lowercase
    # value, so accepting either form here is a faithful check of
    # Requirement 11.4 ("difficulty constrained to a fixed set when
    # present"), cross-verified through ``PromptDifficulty``.
    allowed_raw = {d.name for d in PromptDifficulty} | {d.value for d in PromptDifficulty}
    for (value,) in rows:
        assert value is None or value in allowed_raw, (
            f"unexpected stored difficulty: {value!r}"
        )


# ---------------------------------------------------------------------------
# 5. Packaging sanity
# ---------------------------------------------------------------------------


def test_default_seed_file_exists_inside_persistence_package() -> None:
    assert DEFAULT_SEED_FILE.is_file(), (
        f"packaged seed file is missing at {DEFAULT_SEED_FILE}"
    )
    # Must live inside the app/persistence package directory so a
    # normal ``pip install`` carries it along.
    assert DEFAULT_SEED_FILE.parent.name == "persistence"
    assert DEFAULT_SEED_FILE.parent.parent.name == "app"
