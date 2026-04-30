"""Prompt_Repository — the store-side adapter that supplies Prompts.

Requirements 11.1 / 11.2: "WHEN the Game_Service creates a new Game,
THE Prompt_Repository SHALL supply a Prompt using a defined selection
policy such as random, difficulty-based, or rotation." For v1 we
implement the simplest of the three — uniform random selection over
the seeded ``prompts`` table — behind a single-method interface so the
policy can be swapped out later without touching the Game_Service.

Design notes:

- **Thin wrapper.** The repository does not cache; every
  :meth:`select_prompt` call opens a session via the injected
  ``session_factory`` and runs one SELECT. The lounge scale
  (tens of concurrent players, ~dozens of prompts) makes caching
  unnecessary, and it keeps the implementation honest when the
  policy changes (e.g., to weighted-by-difficulty sampling).
- **Random source injection.** Tests pin the ``random_choice``
  callable so they can assert which row was returned without
  relying on global :mod:`random` state. Default is
  :func:`random.choice`.
- **Returns a detached payload, not the ORM row.** We return a
  frozen dataclass (:class:`SelectedPrompt`) with the fields the
  Game_Service needs (``id``, ``text``, ``language``, ``difficulty``).
  That keeps the service layer from accidentally reattaching an
  ORM instance to a later session.
- **Empty-table handling.** If the ``prompts`` table is empty we
  raise :class:`NoPromptsAvailable` rather than returning ``None``.
  Seed data is loaded at app startup (see
  :mod:`app.persistence.prompt_seed`) so an empty table in practice
  signals a deployment problem, not a recoverable condition.

Requirements addressed:
- 11.1 (Prompt selection policy)
- 11.2 (Returned Prompt carries id, text, language)
"""

from __future__ import annotations

import random as _random_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Prompt, PromptDifficulty


# ---------------------------------------------------------------------------
# Exceptions and result types
# ---------------------------------------------------------------------------


class NoPromptsAvailable(RuntimeError):
    """Raised when :meth:`PromptRepository.select_prompt` finds an empty table.

    In production the ``prompts`` table is populated on startup via
    :func:`app.persistence.prompt_seed.seed_prompts_if_empty`. A runtime
    encounter with this exception indicates that the seed step was
    skipped or the table was manually truncated — both are deployment
    bugs, not user-facing error paths.
    """


@dataclass(frozen=True)
class SelectedPrompt:
    """A Prompt row detached from the ORM session.

    The service layer only needs the payload fields; we expose them as
    a frozen dataclass so a later session cannot be accidentally
    polluted by reattaching an ORM instance. ``difficulty`` is the
    string form (``"easy" | "medium" | "hard" | None``) so callers do
    not need to import the ``PromptDifficulty`` enum.
    """

    id: str
    text: str
    language: str
    difficulty: str | None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def _default_random_choice(seq: Sequence[str]) -> str:
    """Uniform random pick over ``seq``; thin wrapper for injection."""
    return _random_module.choice(seq)


class PromptRepository:
    """Supplies a Prompt to the Game_Service per the configured policy.

    v1 implements uniform random selection. Future policies (difficulty
    weighting, rotation) can be added as alternative selection methods
    or as subclasses without changing the Game_Service contract — the
    service only depends on :meth:`select_prompt` returning one
    :class:`SelectedPrompt`.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        random_choice: Callable[[Sequence[str]], str] = _default_random_choice,
    ) -> None:
        """Initialize the repository.

        Args:
            session_factory: Zero-arg callable returning a fresh
                SQLAlchemy :class:`Session`. Used as a context manager
                per call so the repository holds no long-lived DB
                state.
            random_choice: Callable that picks one element from a
                non-empty sequence of prompt ids. Injected so tests can
                pin selection deterministically. Defaults to
                :func:`random.choice`.
        """
        self._session_factory = session_factory
        self._random_choice = random_choice

    def select_prompt(self) -> SelectedPrompt:
        """Return one Prompt per the selection policy.

        Uniform random over all rows in the ``prompts`` table. We pull
        the id list first and then re-fetch the chosen row by primary
        key; this is cheap for the lounge scale (~dozens of prompts)
        and keeps the random surface on a simple ``list[str]`` so the
        injected ``random_choice`` callable has a stable signature.

        Returns:
            A :class:`SelectedPrompt` snapshot of the chosen row.

        Raises:
            NoPromptsAvailable: If the ``prompts`` table is empty.
        """
        with self._session_factory() as session:
            ids: list[str] = list(
                session.execute(select(Prompt.id)).scalars().all()
            )
            if not ids:
                raise NoPromptsAvailable(
                    "prompts table is empty; seed data was not loaded"
                )

            chosen_id = self._random_choice(ids)
            row = session.get(Prompt, chosen_id)
            if row is None:
                # Vanishingly unlikely: the id we just read was deleted
                # between the SELECT and the re-fetch. Surface the
                # same exception rather than returning None so the
                # caller path is single-branched.
                raise NoPromptsAvailable(
                    f"prompt {chosen_id!r} disappeared during selection"
                )

            difficulty = (
                row.difficulty.value
                if isinstance(row.difficulty, PromptDifficulty)
                else None
            )
            return SelectedPrompt(
                id=row.id,
                text=row.text,
                language=row.language,
                difficulty=difficulty,
            )


__all__ = [
    "NoPromptsAvailable",
    "PromptRepository",
    "SelectedPrompt",
]
