"""Tests for the dynamic typed-text length guard on
``POST /games/{gameId}/result`` (task 9.4 / Requirement 13.2).

The endpoint enforces two bounds on the submitted ``typedText``:

1. A static outer cap at :data:`app.api.schemas.MAX_TYPED_TEXT_LENGTH`
   (1024 chars) enforced by Pydantic; payloads larger than this
   surface as 400 ``validation_error`` at the framework layer before
   the endpoint body runs.
2. A dynamic per-Game cap at
   ``len(prompt.text) + TYPED_TEXT_SLACK_CHARS`` enforced in the
   endpoint body; payloads in ``(prompt_len + slack, 1024]`` surface
   as 400 ``validation_error`` from the :class:`ValidationFailed`
   raise.

Both bounds run **before** any Game state transition or Score write,
satisfying Requirement 13.2's "prevent abuse" framing and echoing
the rate-limit-no-side-effects guarantee in Property 19.
"""

from __future__ import annotations

from sqlalchemy import select

from app.api.schemas import MAX_TYPED_TEXT_LENGTH, TYPED_TEXT_SLACK_CHARS
from app.errors import ErrorCode
from app.persistence.models import Game, Score

from api_helpers import auth_headers, build_test_app, register_player


def _begin_game(client, headers) -> dict:
    created = client.post("/games", headers=headers).json()
    begin = client.post(
        f"/games/{created['gameId']}/begin", headers=headers
    ).json()
    return {"gameId": created["gameId"], "begin": begin}


def test_typed_text_just_within_prompt_plus_slack_succeeds() -> None:
    """A typed text exactly at ``prompt_length + slack`` is accepted."""
    prompt = "a" * 120
    with build_test_app(prompt_text=prompt) as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        info = _begin_game(client, headers)

        max_allowed = len(prompt) + TYPED_TEXT_SLACK_CHARS
        # Fill with valid characters (the prompt text) up to the cap.
        typed = (prompt + "x" * TYPED_TEXT_SLACK_CHARS)[:max_allowed]
        assert len(typed) == max_allowed

        response = client.post(
            f"/games/{info['gameId']}/result",
            json={"typedText": typed},
            headers=headers,
        )
        assert response.status_code == 200, response.text


def test_typed_text_over_prompt_plus_slack_returns_400() -> None:
    """A typed text one character over ``prompt_length + slack`` is rejected."""
    prompt = "a" * 120
    with build_test_app(prompt_text=prompt) as (_, client, session_factory):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        info = _begin_game(client, headers)

        max_allowed = len(prompt) + TYPED_TEXT_SLACK_CHARS
        typed = "y" * (max_allowed + 1)

        response = client.post(
            f"/games/{info['gameId']}/result",
            json={"typedText": typed},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR
        assert body["details"]["field"] == "typedText"
        assert body["details"]["reason"] == "too_long"
        assert body["details"]["promptLength"] == len(prompt)
        assert body["details"]["slack"] == TYPED_TEXT_SLACK_CHARS
        assert body["details"]["maxAllowed"] == max_allowed
        assert body["details"]["actual"] == len(typed)

        # No side effects: the Game stays in_progress (not completed
        # or abandoned), and no Score was written.
        with session_factory() as s:
            game = s.execute(
                select(Game).where(Game.id == info["gameId"])
            ).scalar_one()
            assert game.status.value == "in_progress"
            assert game.ended_at is None
            scores = (
                s.execute(
                    select(Score.id).where(Score.game_id == info["gameId"])
                )
                .scalars()
                .all()
            )
            assert scores == []


def test_typed_text_over_static_cap_rejected_by_pydantic() -> None:
    """A typed text over the static outer cap surfaces as 400 at the framework layer.

    The dynamic per-Game check never runs in this case because the
    Pydantic ``max_length`` validator fires first. That's fine — both
    bounds reject with 400 ``validation_error``; we just confirm the
    outer cap holds.
    """
    prompt = "b" * 120
    with build_test_app(prompt_text=prompt) as (_, client, session_factory):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        info = _begin_game(client, headers)

        typed = "z" * (MAX_TYPED_TEXT_LENGTH + 1)

        response = client.post(
            f"/games/{info['gameId']}/result",
            json={"typedText": typed},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR

        # No side effects: the Game still in_progress; no Score row.
        with session_factory() as s:
            game = s.execute(
                select(Game).where(Game.id == info["gameId"])
            ).scalar_one()
            assert game.status.value == "in_progress"
            scores = (
                s.execute(
                    select(Score.id).where(Score.game_id == info["gameId"])
                )
                .scalars()
                .all()
            )
            assert scores == []
