"""Smoke tests for the scaffolded FastAPI app.

These verify that:
- The app can be imported and built.
- The health placeholder route works.
- The shared ApiError contract is returned for common error cases:
  * 400 validation_error
  * 401 session_expired
  * 404 not_found
  * 409 nickname_taken / game_conflict / game_timeout
  * 429 rate_limited
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.errors import (
    ApiError,
    Conflict,
    ErrorCode,
    GameTimeout,
    NicknameTaken,
    NotFound,
    RateLimited,
    Unauthorized,
    ValidationFailed,
    install_error_handlers,
)
from app.main import create_app


# ---------------------------------------------------------------------------
# Placeholder route smoke test
# ---------------------------------------------------------------------------


def test_health_endpoint_returns_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "environment" in body


# ---------------------------------------------------------------------------
# Error contract tests
# ---------------------------------------------------------------------------


def _app_with_error_routes() -> FastAPI:
    """A minimal app that raises every ApiException variant for verification."""
    app = FastAPI()
    install_error_handlers(app)

    router = APIRouter()

    @router.get("/boom/validation")
    def _validation() -> None:
        raise ValidationFailed("bad input", details={"field": "nickname"})

    @router.get("/boom/unauthorized")
    def _unauthorized() -> None:
        raise Unauthorized("session expired")

    @router.get("/boom/not-found")
    def _not_found() -> None:
        raise NotFound("no such game", details={"gameId": "missing"})

    @router.get("/boom/nickname")
    def _nickname() -> None:
        raise NicknameTaken("nickname already in use")

    @router.get("/boom/game-conflict")
    def _game_conflict() -> None:
        raise Conflict("game already in progress", details={"gameId": "abc"})

    @router.get("/boom/game-timeout")
    def _game_timeout() -> None:
        raise GameTimeout("time's up")

    @router.get("/boom/rate-limited")
    def _rate_limited() -> None:
        raise RateLimited("slow down")

    app.include_router(router)
    return app


def _assert_api_error(body: dict, *, code: str) -> None:
    # Revalidate shape via the shared pydantic model.
    parsed = ApiError.model_validate(body)
    assert parsed.code == code
    assert parsed.message
    # ``details`` is optional; when present it must be a dict.
    if parsed.details is not None:
        assert isinstance(parsed.details, dict)


def test_validation_error_returns_400() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/validation")
    assert response.status_code == 400
    _assert_api_error(response.json(), code=ErrorCode.VALIDATION_ERROR)
    assert response.json()["details"] == {"field": "nickname"}


def test_unauthorized_returns_401() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/unauthorized")
    assert response.status_code == 401
    _assert_api_error(response.json(), code=ErrorCode.SESSION_EXPIRED)


def test_not_found_returns_404() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/not-found")
    assert response.status_code == 404
    _assert_api_error(response.json(), code=ErrorCode.NOT_FOUND)


def test_nickname_taken_returns_409() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/nickname")
    assert response.status_code == 409
    _assert_api_error(response.json(), code=ErrorCode.NICKNAME_TAKEN)


def test_game_conflict_returns_409() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/game-conflict")
    assert response.status_code == 409
    body = response.json()
    _assert_api_error(body, code=ErrorCode.GAME_CONFLICT)
    assert body["details"] == {"gameId": "abc"}


def test_game_timeout_returns_409_with_timeout_code() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/game-timeout")
    assert response.status_code == 409
    _assert_api_error(response.json(), code=ErrorCode.GAME_TIMEOUT)


def test_rate_limited_returns_429() -> None:
    client = TestClient(_app_with_error_routes())
    response = client.get("/boom/rate-limited")
    assert response.status_code == 429
    _assert_api_error(response.json(), code=ErrorCode.RATE_LIMITED)


def test_request_validation_error_uses_api_error_shape() -> None:
    """FastAPI's own validation errors should still emit our envelope."""
    app = FastAPI()
    install_error_handlers(app)

    from pydantic import BaseModel

    class Body(BaseModel):
        nickname: str

    @app.post("/validate")
    def _validate(body: Body) -> dict:
        return {"ok": True, "nickname": body.nickname}

    client = TestClient(app)
    response = client.post("/validate", json={})
    assert response.status_code == 400
    body = response.json()
    _assert_api_error(body, code=ErrorCode.VALIDATION_ERROR)
    assert "errors" in body["details"]
