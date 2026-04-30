"""Pydantic request / response models for the HTTP API.

All JSON on the wire uses ``camelCase`` keys — that's the convention
the React + TypeScript frontend expects — while the Python
attributes stay ``snake_case`` to match the rest of the backend.
This is achieved by populating ``alias`` on every ``Field`` and
configuring the models with ``populate_by_name=True``; FastAPI
serializes responses with ``by_alias=True`` at the router level so
payloads come out in camelCase.

Design notes:

- **Request models** use ``model_config`` with
  ``populate_by_name=True`` so clients can send either the alias
  (camelCase) or the attribute name (snake_case). That keeps
  internal callers like tests writing idiomatic Python while
  matching the documented wire format.
- **Response models** mirror the service-layer result dataclasses,
  but rename fields to the frontend-facing names documented in the
  design (``gameId``, ``sessionToken``, etc.).
- **Datetime fields** are typed as :class:`datetime` so Pydantic
  serializes them to ISO-8601 UTC strings automatically; the API
  always returns timezone-aware values.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------


class _ApiModel(BaseModel):
    """Base model that serialises by alias and accepts either name."""

    model_config = ConfigDict(
        populate_by_name=True,
        # ``from_attributes`` lets us build responses directly from the
        # service-layer dataclasses without a manual dict conversion.
        from_attributes=True,
    )


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


class RegisterPlayerRequest(_ApiModel):
    """Body for ``POST /players``.

    An outer ``max_length`` guard rejects pathologically large
    submissions at the framework layer before the service-level
    validator runs (task 9.3 / Requirement 13.3). The domain
    validator in :mod:`app.domain.nickname` enforces the exact
    ``[2, 20]`` window and charset rules; this guard just caps the
    bytes parsed into memory at a sane upper bound (64 chars) so a
    caller can't force the server to allocate an arbitrarily large
    string before the real validator rejects it.
    """

    nickname: str = Field(
        ...,
        max_length=64,
        description="Display name chosen by the player.",
    )


class RegisterPlayerResponse(_ApiModel):
    """Successful ``POST /players`` response (task 8.1)."""

    player_id: str = Field(..., alias="playerId")
    session_token: str = Field(..., alias="sessionToken")
    nickname: str = Field(...)
    session_expires_at: datetime = Field(..., alias="sessionExpiresAt")


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


class CreateGameResponse(_ApiModel):
    """Successful ``POST /games`` response (task 8.2).

    Returns the Game id, the assigned prompt text, and a server-side
    reference clock the client aligns its countdown with. The prompt
    id is included too so that the frontend can re-fetch metadata via
    ``GET /games/{gameId}`` without another state lookup.
    """

    game_id: str = Field(..., alias="gameId")
    prompt_id: str = Field(..., alias="promptId")
    prompt: str = Field(..., description="Prompt text to type.")
    language: str = Field(...)
    status: str = Field(..., description="Current Game status.")
    start_at: datetime = Field(..., alias="startAt")


class BeginGameResponse(_ApiModel):
    """Successful ``POST /games/{gameId}/begin`` response (task 8.3).

    Includes the authoritative ``startedAt`` that the Scoring_Service
    will later use for elapsed-time calculation (Requirements
    15.1 / 15.2).
    """

    game_id: str = Field(..., alias="gameId")
    status: str = Field(...)
    started_at: datetime = Field(..., alias="startedAt")
    prompt_id: str = Field(..., alias="promptId")
    prompt: str = Field(...)


#: Defensive outer bound on ``typedText`` length on ``POST /games/{gameId}/result``.
#:
#: Task 9.4 / Requirement 13.2: the typed text is echoed through the
#: scoring pipeline and could in principle be rendered anywhere the
#: Game surfaces (e.g. a debug tool). Prompts are at most 500 chars
#: (Requirement 11.3); we accept up to ~2x that as a static upper
#: bound so a malicious client can't force the router to parse a
#: multi-megabyte body. The *primary* per-request check is dynamic:
#: the ``POST /games/{gameId}/result`` endpoint fetches the Game's
#: prompt length and rejects any typed text longer than
#: ``prompt_length + TYPED_TEXT_SLACK_CHARS`` (see
#: :data:`TYPED_TEXT_SLACK_CHARS`). This static cap is just a belt-
#: and-suspenders outer bound.
MAX_TYPED_TEXT_LENGTH: int = 1024


#: Slack in characters allowed beyond the prompt length on
#: ``POST /games/{gameId}/result`` (task 9.4 / Requirement 13.2).
#:
#: Real players may overshoot the prompt slightly due to typos,
#: rapid key presses, or auto-repeat. We allow a small overshoot
#: (50 chars) before flagging the submission as abusive. Anything
#: larger is rejected with 400 ``validation_error`` before the
#: scoring pipeline touches it.
TYPED_TEXT_SLACK_CHARS: int = 50


class SubmitResultRequest(_ApiModel):
    """Body for ``POST /games/{gameId}/result`` (task 8.4).

    ``elapsedSeconds`` is accepted for backward-compat with older
    clients but explicitly ignored by the scoring layer per
    Requirement 3.6 / 15.2 / Property 7. The server-measured elapsed
    time (``endedAt - startedAt``) is what drives the score.

    ``typedText`` has two bounds (task 9.4 / Requirement 13.2):

    - A static outer cap at :data:`MAX_TYPED_TEXT_LENGTH` enforced
      here by Pydantic so abusive multi-megabyte bodies never reach
      the endpoint. Over this bound → 400 ``validation_error`` at
      the framework layer.
    - A dynamic per-Game cap at
      ``len(prompt.text) + TYPED_TEXT_SLACK_CHARS`` enforced in the
      ``submit_result`` endpoint body. This is the primary guard
      against abuse; the static cap is belt-and-suspenders.
    """

    typed_text: str = Field(
        ...,
        alias="typedText",
        max_length=MAX_TYPED_TEXT_LENGTH,
    )
    elapsed_seconds: float | None = Field(
        default=None,
        alias="elapsedSeconds",
        description=(
            "Client-observed elapsed time in seconds. Accepted for "
            "backward compatibility but ignored during scoring."
        ),
    )


class SubmitResultResponse(_ApiModel):
    """Successful ``POST /games/{gameId}/result`` response (task 8.4).

    Matches Requirement 4.6 / 4.7: the player sees ``wpm``,
    ``accuracy``, ``points``, and their current ``rank`` on the
    leaderboard.
    """

    game_id: str = Field(..., alias="gameId")
    wpm: float
    accuracy: float
    points: int
    rank: int = Field(
        ...,
        description=(
            "1-based rank on the current leaderboard after this Score. "
            "Rank is the contiguous sequence defined by LeaderboardService."
        ),
    )
    ended_at: datetime = Field(..., alias="endedAt")


class GameMetadataResponse(_ApiModel):
    """Successful ``GET /games/{gameId}`` response (task 8.6)."""

    game_id: str = Field(..., alias="gameId")
    player_id: str = Field(..., alias="playerId")
    prompt_id: str = Field(..., alias="promptId")
    prompt: str = Field(..., description="Prompt text to type.")
    language: str = Field(...)
    status: str = Field(...)
    started_at: datetime | None = Field(default=None, alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


class LeaderboardEntryResponse(_ApiModel):
    """One row of ``GET /leaderboard`` (task 8.5).

    Field names mirror :class:`app.services.leaderboard_service.LeaderboardEntry`
    and the design Model 5 contract for Dashboard_Client rendering
    (Requirement 6.2).
    """

    player_id: str = Field(..., alias="playerId")
    nickname: str = Field(...)
    best_wpm: float = Field(..., alias="bestWpm")
    best_accuracy: float = Field(..., alias="bestAccuracy")
    best_points: int = Field(..., alias="bestPoints")
    rank: int = Field(...)


class LeaderboardResponse(_ApiModel):
    """Full ``GET /leaderboard`` payload (task 8.5)."""

    entries: list[LeaderboardEntryResponse] = Field(default_factory=list)
    generated_at: datetime = Field(..., alias="generatedAt")


__all__ = [
    "BeginGameResponse",
    "CreateGameResponse",
    "GameMetadataResponse",
    "LeaderboardEntryResponse",
    "LeaderboardResponse",
    "MAX_TYPED_TEXT_LENGTH",
    "RegisterPlayerRequest",
    "RegisterPlayerResponse",
    "SubmitResultRequest",
    "SubmitResultResponse",
    "TYPED_TEXT_SLACK_CHARS",
]
