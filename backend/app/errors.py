"""Shared error-response contract and FastAPI exception handlers.

All API errors flow through an ``ApiError`` envelope so that the frontend
and backend can agree on a single shape. Status-code mapping follows the
design document:

- 400 — validation failure (``validation_error``)
- 401 — missing / unknown / expired Session_Token (``session_expired``)
- 404 — unknown resource, e.g. gameId (``not_found``)
- 409 — domain conflict (``nickname_taken``, ``game_conflict``, ``game_timeout``)
- 429 — rate limit exceeded (``rate_limited``)

Requirements addressed:
- 1.7 / 1.8 (duplicate nickname, validation error surfacing)
- 2.6       (game already in progress)
- 7.3       (expired / missing session token)
- 9.2       (timeout submission)
- 12.2      (unknown gameId)
- 14.3      (rate-limit-exceeded responses)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Error codes (stable identifiers shared with the frontend)
# ---------------------------------------------------------------------------


class ErrorCode:
    """Stable, machine-readable error codes returned in ``ApiError.code``."""

    VALIDATION_ERROR = "validation_error"
    NICKNAME_TAKEN = "nickname_taken"
    SESSION_EXPIRED = "session_expired"
    NOT_FOUND = "not_found"
    GAME_CONFLICT = "game_conflict"
    GAME_TIMEOUT = "game_timeout"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


# ---------------------------------------------------------------------------
# Wire-format model
# ---------------------------------------------------------------------------


class ApiError(BaseModel):
    """Envelope returned for every non-2xx API response.

    The frontend mirrors this shape in ``src/api/types.ts``.
    """

    code: str = Field(..., description="Stable machine-readable error code.")
    message: str = Field(..., description="Human-readable error message.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context (e.g. existing gameId on conflict).",
    )


# ---------------------------------------------------------------------------
# Domain exception hierarchy
# ---------------------------------------------------------------------------


class ApiException(Exception):
    """Base class for exceptions that map to an ``ApiError`` response.

    Subclasses pick the HTTP status and error code; callers can pass a
    human-readable ``message`` and an optional ``details`` mapping.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_api_error(self) -> ApiError:
        return ApiError(code=self.code, message=self.message, details=self.details)


class ValidationFailed(ApiException):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.VALIDATION_ERROR


class Unauthorized(ApiException):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.SESSION_EXPIRED


class NotFound(ApiException):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND


class Conflict(ApiException):
    """409 Conflict. Default code is ``game_conflict``.

    Use ``NicknameTaken`` for duplicate-nickname cases, or pass an explicit
    ``code`` (e.g. ``ErrorCode.GAME_TIMEOUT``) for other conflict variants.
    """

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.GAME_CONFLICT


class NicknameTaken(Conflict):
    code = ErrorCode.NICKNAME_TAKEN


class GameTimeout(Conflict):
    code = ErrorCode.GAME_TIMEOUT


class RateLimited(ApiException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------


def _json(error: ApiError, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(error.model_dump(exclude_none=True)),
    )


async def _handle_api_exception(_: Request, exc: ApiException) -> JSONResponse:
    return _json(exc.to_api_error(), exc.status_code)


async def _handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
    code = {
        status.HTTP_400_BAD_REQUEST: ErrorCode.VALIDATION_ERROR,
        status.HTTP_401_UNAUTHORIZED: ErrorCode.SESSION_EXPIRED,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_409_CONFLICT: ErrorCode.GAME_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)

    message = (
        exc.detail
        if isinstance(exc.detail, str)
        else "Request could not be completed."
    )
    details = exc.detail if isinstance(exc.detail, dict) else None
    return _json(ApiError(code=code, message=message, details=details), exc.status_code)


async def _handle_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return _json(
        ApiError(
            code=ErrorCode.VALIDATION_ERROR,
            message="Request payload failed validation.",
            details={"errors": exc.errors()},
        ),
        status.HTTP_400_BAD_REQUEST,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register ApiError-shaped handlers on a FastAPI app."""
    app.add_exception_handler(ApiException, _handle_api_exception)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
