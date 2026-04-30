"""Seed-data loader for the Prompt_Repository (task 2.4).

Startup hook that reads :data:`DEFAULT_SEED_FILE` (a JSON list of prompt
dicts packaged next to this module), runs every entry through
:func:`app.domain.prompt.validate_prompt`, and — on first boot when the
``prompts`` table is empty — inserts one :class:`Prompt` row per entry.

Design choices:

- **Loud failure on invalid seed data.** The seed file is
  developer-controlled, so a validation error is a build-time bug, not
  a runtime surprise. :func:`load_seed_prompts` raises a
  :class:`ValueError` naming the offending index and the specific
  validator result, which surfaces clearly in the startup log.
- **Idempotent at the DB layer.** :func:`seed_prompts_if_empty` skips
  the insert entirely when the ``prompts`` table already has at least
  one row, so a long-running lounge deployment is not re-seeded on
  every restart.
- **Atomic inserts.** All rows are staged with a single
  ``session.add_all`` and committed in one transaction so a partial
  failure rolls back cleanly via the :class:`~sqlalchemy.orm.Session`
  context-manager's rollback-on-exit hook.
- **No FastAPI imports.** This module imports SQLAlchemy and the ORM
  models directly; it is invoked from the FastAPI lifespan in
  :mod:`app.main` but does not depend on FastAPI itself. That makes it
  easy to exercise from plain pytest tests against an in-memory engine.

Requirements addressed:
- 11.2 (Prompt non-empty text and language)
- 11.3 (Prompt text length ``[100, 500]``)
- 11.4 (Difficulty constrained when present)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..domain.prompt import (
    DifficultyError,
    LanguageError,
    OkPrompt,
    PromptValidationResult,
    TextError,
    validate_prompt,
)
from .models import Prompt, PromptDifficulty

#: Packaged seed file. Located alongside this module so a standard
#: ``pip install`` of the backend package carries it along.
DEFAULT_SEED_FILE: Path = Path(__file__).resolve().parent / "seed_prompts.json"


# ---------------------------------------------------------------------------
# JSON → validated OkPrompt list
# ---------------------------------------------------------------------------


def _describe_failure(index: int, entry: object, result: PromptValidationResult) -> str:
    """Format a one-line description of a validation failure.

    The loader wraps this message in a :class:`ValueError`; keeping it
    pure makes it trivially testable and easy to grep for in logs.
    """
    if isinstance(result, TextError):
        return (
            f"seed prompt at index {index} has invalid text: reason={result.reason!r}, "
            f"length={result.length}, allowed={result.min_length}-{result.max_length}"
        )
    if isinstance(result, DifficultyError):
        return (
            f"seed prompt at index {index} has invalid difficulty: value={result.value!r}, "
            f"allowed={list(result.allowed)}"
        )
    if isinstance(result, LanguageError):
        return (
            f"seed prompt at index {index} has invalid language: value={result.value!r}"
        )
    # Fallback: should not happen because OkPrompt is filtered before
    # this function is called, but keeping the branch makes mypy happy
    # and guards against future additions to the result union.
    return f"seed prompt at index {index} failed validation: {result!r} (entry={entry!r})"


def load_seed_prompts(path: Path | None = None) -> list[OkPrompt]:
    """Read and validate the packaged seed JSON file.

    Args:
        path: Optional override for the JSON path. Defaults to
            :data:`DEFAULT_SEED_FILE`. Tests use this to point at a
            temporary file containing invalid data.

    Returns:
        A list of :class:`OkPrompt` in file order. Every returned
        entry has satisfied the full :func:`validate_prompt` contract,
        so callers can safely write them into the ``prompts`` table
        without re-validating.

    Raises:
        FileNotFoundError: If ``path`` (or :data:`DEFAULT_SEED_FILE`)
            does not exist.
        ValueError: If the JSON is not a list, if any entry is not a
            dict, or if any entry fails :func:`validate_prompt`. The
            message always names the offending zero-based index.
    """
    seed_path = path if path is not None else DEFAULT_SEED_FILE

    with seed_path.open(encoding="utf-8") as fp:
        raw = json.load(fp)

    if not isinstance(raw, list):
        raise ValueError(
            f"seed prompts file {seed_path} must contain a JSON list, got {type(raw).__name__}"
        )

    validated: list[OkPrompt] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"seed prompt at index {index} must be a JSON object, got {type(entry).__name__}"
            )

        # Pull only the fields the validator knows about; extra keys are
        # ignored so the file format can grow later (e.g. tags) without
        # breaking older loaders.
        text = entry.get("text")
        difficulty = entry.get("difficulty")  # may be missing entirely
        language = entry.get("language")

        # ``entry.get`` returns None for missing keys; the validator
        # will reject non-str values via TypeError. Translate those
        # into a uniform ValueError so the caller sees a consistent
        # "invalid seed entry" diagnosis regardless of which rule
        # fired.
        try:
            result = validate_prompt(
                text=text,  # type: ignore[arg-type]
                difficulty=difficulty,  # type: ignore[arg-type]
                language=language,  # type: ignore[arg-type]
            )
        except TypeError as exc:
            raise ValueError(
                f"seed prompt at index {index} has a field with an unexpected type: {exc}"
            ) from exc

        if not isinstance(result, OkPrompt):
            raise ValueError(_describe_failure(index, entry, result))

        validated.append(result)

    return validated


# ---------------------------------------------------------------------------
# DB insert (idempotent)
# ---------------------------------------------------------------------------


def _difficulty_to_enum(value: str | None) -> PromptDifficulty | None:
    """Map a validator-difficulty string back to the ORM enum value."""
    if value is None:
        return None
    # The validator has already restricted ``value`` to
    # ALLOWED_DIFFICULTIES, so this lookup is total.
    return PromptDifficulty(value)


def seed_prompts_if_empty(
    engine: Engine,
    *,
    path: Path | None = None,
) -> int:
    """Insert the packaged seed prompts if the ``prompts`` table is empty.

    Args:
        engine: SQLAlchemy engine bound to the target database. The
            caller is responsible for having run :func:`init_db`
            beforehand.
        path: Optional override passed through to
            :func:`load_seed_prompts`.

    Returns:
        The number of rows inserted. When the ``prompts`` table
        already has at least one row, returns ``0`` and performs no
        writes — this keeps long-running lounge deployments from
        re-seeding on every restart.

    Raises:
        ValueError: Propagated from :func:`load_seed_prompts` if the
            JSON file is malformed or contains an invalid entry.
    """
    with Session(engine) as session:
        # Short-circuit on any existing row. A ``LIMIT 1`` keeps the
        # check cheap even if the table grows large.
        existing = session.execute(select(Prompt.id).limit(1)).first()
        if existing is not None:
            return 0

        prompts = load_seed_prompts(path)

        # SQLAlchemy 2.x auto-begins a transaction on the first query
        # above, so we reuse that transaction for the writes. Adding
        # all rows before a single ``commit`` keeps the batch atomic:
        # any error on flush/commit rolls back the whole batch via
        # the Session's ``__exit__`` hook, leaving the table empty
        # rather than half-populated.
        session.add_all(
            Prompt(
                id=str(uuid.uuid4()),
                text=ok.text,
                difficulty=_difficulty_to_enum(ok.difficulty),
                language=ok.language,
            )
            for ok in prompts
        )
        session.commit()

        return len(prompts)


__all__ = [
    "DEFAULT_SEED_FILE",
    "load_seed_prompts",
    "seed_prompts_if_empty",
]
