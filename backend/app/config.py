"""Environment-driven runtime configuration.

All tunables that influence game rules, session lifetime, rate limits,
and prompt selection live here so they can be adjusted per deployment
without code changes.

Requirements addressed:
- 7.5  (Session_Token bounded lifetime)
- 9.1  (Maximum_Game_Duration)
- 14.1 / 14.2 (Rate limits for POST /players and POST /games)
- 11.1 (Prompt selection policy)
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PromptSelectionPolicy(str, Enum):
    """Policy used by the Prompt_Repository to pick a prompt for a new Game."""

    RANDOM = "random"
    DIFFICULTY = "difficulty"
    ROTATION = "rotation"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Environment variables are prefixed with ``TYPING_GAME_``.
    Example: ``TYPING_GAME_SESSION_TTL_SECONDS=1800``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TYPING_GAME_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Session lifecycle (Requirement 7.5) ---
    session_ttl_seconds: int = Field(
        default=30 * 60,
        ge=1,
        description="Bounded lifetime of a Session_Token in seconds.",
    )

    # --- Game timing (Requirement 9.1) ---
    max_game_duration_seconds: int = Field(
        default=120,
        ge=1,
        description="Server-side upper bound on a Game's typing phase (Maximum_Game_Duration).",
    )
    timeout_sweeper_interval_seconds: int = Field(
        default=5,
        ge=1,
        description="How often the background sweeper scans for timed-out in_progress Games.",
    )

    # --- Rate limits (Requirements 14.1, 14.2) ---
    rate_limit_players_per_ip_per_minute: int = Field(
        default=10,
        ge=1,
        description="Token-bucket rate limit on POST /players per source IP.",
    )
    rate_limit_games_per_ip_per_minute: int = Field(
        default=30,
        ge=1,
        description="Token-bucket rate limit on POST /games per source IP.",
    )
    rate_limit_games_per_player_per_minute: int = Field(
        default=10,
        ge=1,
        description="Token-bucket rate limit on POST /games per playerId.",
    )

    # --- Prompt selection (Requirement 11.1) ---
    prompt_selection_policy: PromptSelectionPolicy = Field(
        default=PromptSelectionPolicy.RANDOM,
        description="Policy used by the Prompt_Repository to pick a prompt.",
    )

    # --- Persistence ---
    database_url: str = Field(
        default="sqlite:///./typing_game.db",
        description="SQLAlchemy database URL.",
    )

    # --- Misc ---
    environment: str = Field(
        default="development",
        description="Free-form environment label (development, test, production).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Cached so importing modules share a single view of the environment.
    Tests that need to override values should call
    ``get_settings.cache_clear()`` after mutating env vars.
    """
    return Settings()
