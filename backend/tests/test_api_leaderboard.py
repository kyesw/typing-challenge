"""HTTP-level tests for ``GET /leaderboard`` (task 8.5)."""

from __future__ import annotations

from app.errors import ErrorCode

from api_helpers import auth_headers, build_test_app, register_player


def test_empty_leaderboard_is_empty_list() -> None:
    with build_test_app() as (_, client, _):
        response = client.get("/leaderboard")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["entries"] == []
        assert body["generatedAt"]


def test_leaderboard_lists_completed_games() -> None:
    """After a full register → start → begin → submit flow the player appears."""
    prompt = "hello world " * 12  # 144 chars
    with build_test_app(prompt_text=prompt) as (_, client, _):
        reg = register_player(client, "Tracy")
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        client.post(f"/games/{created['gameId']}/begin", headers=headers)
        result = client.post(
            f"/games/{created['gameId']}/result",
            json={"typedText": prompt},
            headers=headers,
        )
        assert result.status_code == 200, result.text

        response = client.get("/leaderboard")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["playerId"] == reg["playerId"]
        assert entry["nickname"] == "Tracy"
        assert entry["rank"] == 1
        assert entry["bestWpm"] >= 0
        assert 0 <= entry["bestAccuracy"] <= 100
        assert entry["bestPoints"] >= 0


def test_leaderboard_does_not_require_auth() -> None:
    """``GET /leaderboard`` is read-only and public per Requirement 5.6."""
    with build_test_app() as (_, client, _):
        response = client.get("/leaderboard")
        assert response.status_code == 200
        body = response.json()
        # Even without an auth header we do not see a session_expired
        # envelope — the endpoint is not protected.
        assert "code" not in body or body.get("code") != ErrorCode.SESSION_EXPIRED
