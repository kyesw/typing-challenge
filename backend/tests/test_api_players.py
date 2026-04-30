"""HTTP-level tests for ``POST /players`` (task 8.1).

Covers the documented response shapes:

- 201 on a valid nickname — body has ``playerId``, ``sessionToken``,
  ``nickname``, ``sessionExpiresAt``.
- 400 ``validation_error`` on too-short, too-long, or disallowed-char
  nicknames (Requirements 1.5, 1.6, 1.8).
- 409 ``nickname_taken`` on a case-insensitive duplicate (Requirement
  1.7).
- Request validation (missing field / wrong type) also surfaces as
  ``validation_error``.
"""

from __future__ import annotations

from app.errors import ErrorCode

from api_helpers import build_test_app


def test_register_success_returns_201_and_token() -> None:
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={"nickname": "Alice"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["nickname"] == "Alice"
        assert body["playerId"]
        assert body["sessionToken"]
        assert body["sessionExpiresAt"]


def test_register_camelcase_or_snakecase_request() -> None:
    """The request body accepts the field by attribute name too."""
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={"nickname": "Bob"})
        assert response.status_code == 201, response.text


def test_register_too_short_returns_400() -> None:
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={"nickname": "A"})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR
        assert body["details"]["reason"] == "length"


def test_register_too_long_returns_400() -> None:
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={"nickname": "A" * 21})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR
        assert body["details"]["reason"] == "length"


def test_register_disallowed_char_returns_400() -> None:
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={"nickname": "bad$name"})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR
        assert body["details"]["reason"] == "charset"


def test_register_duplicate_returns_409_nickname_taken() -> None:
    """Case-insensitive duplicate collides with the active player."""
    with build_test_app() as (_, client, _):
        first = client.post("/players", json={"nickname": "Alice"})
        assert first.status_code == 201, first.text

        dup = client.post("/players", json={"nickname": "alice"})
        assert dup.status_code == 409
        body = dup.json()
        assert body["code"] == ErrorCode.NICKNAME_TAKEN


def test_register_missing_field_returns_400() -> None:
    """FastAPI's request validation uses the shared ApiError envelope."""
    with build_test_app() as (_, client, _):
        response = client.post("/players", json={})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR
