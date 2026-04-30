"""Smoke tests for env-driven settings."""

from __future__ import annotations

from app.config import PromptSelectionPolicy, Settings, get_settings


def test_default_settings_are_sane() -> None:
    s = Settings()
    assert s.session_ttl_seconds > 0
    assert s.max_game_duration_seconds > 0
    assert s.rate_limit_players_per_ip_per_minute > 0
    assert s.rate_limit_games_per_ip_per_minute > 0
    assert s.rate_limit_games_per_player_per_minute > 0
    assert s.prompt_selection_policy in PromptSelectionPolicy


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TYPING_GAME_SESSION_TTL_SECONDS", "42")
    monkeypatch.setenv("TYPING_GAME_MAX_GAME_DURATION_SECONDS", "7")
    monkeypatch.setenv("TYPING_GAME_PROMPT_SELECTION_POLICY", "rotation")

    # Bypass the cache so we actually read the overridden env.
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.session_ttl_seconds == 42
        assert s.max_game_duration_seconds == 7
        assert s.prompt_selection_policy is PromptSelectionPolicy.ROTATION
    finally:
        get_settings.cache_clear()
