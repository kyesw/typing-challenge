"""Persistence package: SQLAlchemy models, engine, and schema helpers.

Exposes the ORM models and the ``init_db`` helper used by the FastAPI
startup path and by tests to materialize the schema against an in-memory
SQLite engine.

Requirements addressed:
- 1.3, 2.3, 4.4, 4.5, 8.7, 11.2, 16.2 (data model shapes and invariants)
"""

from __future__ import annotations

from .base import Base
from .engine import create_engine_from_settings, get_sessionmaker
from .models import Game, GameStatus, Player, Prompt, PromptDifficulty, Score
from .prompt_repository import (
    NoPromptsAvailable,
    PromptRepository,
    SelectedPrompt,
)
from .prompt_seed import DEFAULT_SEED_FILE, load_seed_prompts, seed_prompts_if_empty
from .schema import init_db

__all__ = [
    "Base",
    "DEFAULT_SEED_FILE",
    "Game",
    "GameStatus",
    "NoPromptsAvailable",
    "Player",
    "Prompt",
    "PromptDifficulty",
    "PromptRepository",
    "Score",
    "SelectedPrompt",
    "create_engine_from_settings",
    "get_sessionmaker",
    "init_db",
    "load_seed_prompts",
    "seed_prompts_if_empty",
]
