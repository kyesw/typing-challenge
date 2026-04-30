"""``POST /players`` — nickname registration (task 8.1).

Flow:

1. Validate the body with :class:`RegisterPlayerRequest`. FastAPI
   handles the pydantic failure via the shared validation handler
   which already produces an ``ApiError(code=validation_error)``
   envelope, so we don't need to translate it ourselves.
2. Hand the nickname to :meth:`PlayerService.register`.
3. Map the service result:
   * :class:`RegistrationSuccess` → 201 with the documented payload.
   * :class:`NicknameValidationError` → 400 ``validation_error``
     (Requirements 1.5, 1.6, 1.8).
   * :class:`NicknameTaken` → 409 ``nickname_taken`` (Requirements
     1.7, 1.8).

The endpoint is *not* protected by :func:`require_player` — it's the
path that mints the token in the first place.

Requirements addressed:
- 1.2, 1.3 (POST body → PlayerService → playerId + sessionToken)
- 1.5, 1.6 (length + character-set rules)
- 1.7     (case-insensitive uniqueness among Active_Players)
- 1.8     (validation / duplicate surface as ApiError for the client)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..errors import (
    ErrorCode,
    NicknameTaken as NicknameTakenApiError,
    ValidationFailed,
)
from ..services import (
    NicknameTaken,
    NicknameValidationError,
    RegistrationSuccess,
)
from .dependencies import PlayerServiceDep, enforce_players_rate_limit
from .schemas import RegisterPlayerRequest, RegisterPlayerResponse


router = APIRouter(tags=["players"])


@router.post(
    "/players",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterPlayerResponse,
    response_model_by_alias=True,
    dependencies=[Depends(enforce_players_rate_limit)],
)
def register_player(
    body: RegisterPlayerRequest,
    player_service: PlayerServiceDep,
) -> RegisterPlayerResponse:
    """Register a new player and return the issued Session_Token."""
    result = player_service.register(body.nickname)

    if isinstance(result, NicknameValidationError):
        # The validator distinguishes "length" vs "charset" so the
        # client can localise the message differently; we pass both
        # through in ``details`` and pick a concise top-level
        # message.
        if result.code == "length":
            message = "Nickname must be between 2 and 20 characters."
        else:
            message = (
                "Nickname may only contain letters, digits, spaces, "
                "hyphens, and underscores."
            )
        raise ValidationFailed(
            message,
            details={"field": "nickname", "reason": result.code, **result.details},
            code=ErrorCode.VALIDATION_ERROR,
        )

    if isinstance(result, NicknameTaken):
        raise NicknameTakenApiError(
            "That nickname is already in use.",
            details={"nicknameCi": result.nickname_ci},
        )

    assert isinstance(result, RegistrationSuccess)
    return RegisterPlayerResponse(
        player_id=result.player_id,
        session_token=result.session_token,
        nickname=result.nickname,
        session_expires_at=result.session_expires_at,
    )


__all__ = ["router"]
