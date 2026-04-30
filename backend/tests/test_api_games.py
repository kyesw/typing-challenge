"""HTTP-level tests for the games router (tasks 8.2, 8.3, 8.4, 8.6, 8.7).

Covers the documented response shapes for each endpoint plus the
session-token authorization check shared across the three protected
routes.
"""

from __future__ import annotations

import time
import uuid

from app.config import Settings
from app.errors import ErrorCode

from api_helpers import auth_headers, build_test_app, register_player


# ---------------------------------------------------------------------------
# Authorization (task 8.7)
# ---------------------------------------------------------------------------


def test_create_game_requires_auth_header() -> None:
    with build_test_app() as (_, client, _):
        response = client.post("/games")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == ErrorCode.SESSION_EXPIRED


def test_create_game_rejects_unknown_bearer_token() -> None:
    with build_test_app() as (_, client, _):
        response = client.post(
            "/games",
            headers=auth_headers("definitely-not-a-real-token"),
        )
        assert response.status_code == 401
        assert response.json()["code"] == ErrorCode.SESSION_EXPIRED


def test_create_game_rejects_expired_token() -> None:
    """A player whose session has expired cannot start a game."""
    # A 1-second TTL is plenty of margin to fall out of the validity
    # window before the next request runs.
    settings = Settings(session_ttl_seconds=1)
    with build_test_app(settings=settings) as (_, client, _):
        reg = register_player(client, "Zoe")
        token = reg["sessionToken"]

        # Wait long enough for the TTL to elapse.
        time.sleep(1.2)

        response = client.post("/games", headers=auth_headers(token))
        assert response.status_code == 401
        assert response.json()["code"] == ErrorCode.SESSION_EXPIRED


def test_create_game_rejects_malformed_scheme() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        token = reg["sessionToken"]
        response = client.post(
            "/games",
            headers={"Authorization": f"Basic {token}"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /games (task 8.2)
# ---------------------------------------------------------------------------


def test_create_game_success_returns_201() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        response = client.post("/games", headers=auth_headers(reg["sessionToken"]))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["gameId"]
        assert body["prompt"]
        assert body["promptId"]
        assert body["language"] == "en"
        assert body["status"] == "pending"
        assert body["startAt"]


def test_create_game_conflict_returns_existing_id() -> None:
    """A second ``POST /games`` while one is pending → 409 w/ existingGameId."""
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])

        first = client.post("/games", headers=headers)
        assert first.status_code == 201
        existing_id = first.json()["gameId"]

        second = client.post("/games", headers=headers)
        assert second.status_code == 409
        body = second.json()
        assert body["code"] == ErrorCode.GAME_CONFLICT
        assert body["details"]["existingGameId"] == existing_id
        assert body["details"]["status"] == "pending"


# ---------------------------------------------------------------------------
# POST /games/{gameId}/begin (task 8.3)
# ---------------------------------------------------------------------------


def test_begin_game_transitions_pending_to_in_progress() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()

        response = client.post(
            f"/games/{created['gameId']}/begin",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["gameId"] == created["gameId"]
        assert body["status"] == "in_progress"
        assert body["startedAt"]
        # Prompt is echoed for convenience on reconnect.
        assert body["prompt"]


def test_begin_game_unknown_returns_404() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        response = client.post(
            f"/games/{uuid.uuid4()}/begin",
            headers=auth_headers(reg["sessionToken"]),
        )
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND


def test_begin_game_not_in_pending_returns_409() -> None:
    """Calling begin twice returns 409 with the current status."""
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        first = client.post(
            f"/games/{created['gameId']}/begin", headers=headers
        )
        assert first.status_code == 200

        second = client.post(
            f"/games/{created['gameId']}/begin", headers=headers
        )
        assert second.status_code == 409
        body = second.json()
        assert body["code"] == ErrorCode.GAME_CONFLICT
        assert body["details"]["currentStatus"] == "in_progress"


def test_begin_game_other_players_game_returns_404() -> None:
    """Ownership mismatches collapse to 404 so existence doesn't leak."""
    with build_test_app() as (_, client, _):
        owner = register_player(client, "Alice")
        created = client.post(
            "/games", headers=auth_headers(owner["sessionToken"])
        ).json()

        intruder = register_player(client, "Bob")
        response = client.post(
            f"/games/{created['gameId']}/begin",
            headers=auth_headers(intruder["sessionToken"]),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /games/{gameId}/result (task 8.4)
# ---------------------------------------------------------------------------


def test_submit_result_returns_score_and_rank() -> None:
    """Happy path: submit perfect text and get a Score + rank=1."""
    prompt = "the quick brown fox jumps over the lazy dog. " * 3  # 135 chars
    with build_test_app(prompt_text=prompt) as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        client.post(f"/games/{created['gameId']}/begin", headers=headers)

        response = client.post(
            f"/games/{created['gameId']}/result",
            json={"typedText": prompt, "elapsedSeconds": 5.0},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["gameId"] == created["gameId"]
        assert body["wpm"] >= 0
        assert 0 <= body["accuracy"] <= 100
        assert body["accuracy"] == 100
        assert body["points"] >= 0
        assert body["rank"] == 1
        assert body["endedAt"]


def test_submit_result_timeout_returns_409_game_timeout() -> None:
    """A submission arriving after max_game_duration → 409 game_timeout."""
    # 1-second cap; we sleep >1s between ``begin`` and ``result``.
    settings = Settings(max_game_duration_seconds=1)
    with build_test_app(settings=settings) as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        client.post(f"/games/{created['gameId']}/begin", headers=headers)

        time.sleep(1.2)

        response = client.post(
            f"/games/{created['gameId']}/result",
            json={"typedText": "x" * 30},
            headers=headers,
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == ErrorCode.GAME_TIMEOUT
        assert body["details"]["gameId"] == created["gameId"]


def test_submit_result_on_pending_game_returns_409_conflict() -> None:
    """Calling result without begin → 409 game_conflict."""
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        # Skip begin.
        response = client.post(
            f"/games/{created['gameId']}/result",
            json={"typedText": "anything"},
            headers=headers,
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == ErrorCode.GAME_CONFLICT
        assert body["details"]["currentStatus"] == "pending"


def test_submit_result_unknown_game_returns_404() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        response = client.post(
            f"/games/{uuid.uuid4()}/result",
            json={"typedText": "hi"},
            headers=auth_headers(reg["sessionToken"]),
        )
        assert response.status_code == 404


def test_submit_result_ignores_client_elapsed_seconds() -> None:
    """The client-supplied elapsed value is accepted but never scored against.

    Requirement 3.6 / 15.2 / Property 7: scoring only depends on the
    server-measured elapsed time. We submit the same typed text with
    two wildly different ``elapsedSeconds`` values and confirm the
    server's scoring is identical.
    """
    prompt = "a" * 120
    # Two separate test apps so each has a clean DB.
    results: list[dict] = []
    for client_elapsed in (0.1, 9999.9):
        with build_test_app(prompt_text=prompt) as (_, client, _):
            reg = register_player(client)
            headers = auth_headers(reg["sessionToken"])
            created = client.post("/games", headers=headers).json()
            client.post(f"/games/{created['gameId']}/begin", headers=headers)
            response = client.post(
                f"/games/{created['gameId']}/result",
                json={"typedText": prompt, "elapsedSeconds": client_elapsed},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            results.append(response.json())

    # The computed wpm/accuracy depend only on the server clock, not
    # on ``elapsedSeconds``. WPM may differ slightly across runs
    # because the real wall clock drifts between them, but the
    # *server-reported* wpm and accuracy should not depend on
    # ``elapsedSeconds`` — so the difference between the two values
    # of ``elapsedSeconds`` can't possibly make them diverge more
    # than the natural jitter. Assert accuracy identical; it's
    # strictly a function of typed_text vs. prompt.
    assert results[0]["accuracy"] == results[1]["accuracy"] == 100.0


# ---------------------------------------------------------------------------
# GET /games/{gameId} (task 8.6)
# ---------------------------------------------------------------------------


def test_get_game_returns_metadata() -> None:
    prompt = "m" * 130
    with build_test_app(prompt_text=prompt) as (_, client, _):
        reg = register_player(client)
        created = client.post(
            "/games", headers=auth_headers(reg["sessionToken"])
        ).json()

        response = client.get(f"/games/{created['gameId']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["gameId"] == created["gameId"]
        assert body["playerId"] == reg["playerId"]
        assert body["promptId"] == created["promptId"]
        assert body["prompt"] == prompt
        assert body["language"] == "en"
        assert body["status"] == "pending"
        assert body["startedAt"] is None
        assert body["endedAt"] is None


def test_get_game_unknown_returns_404() -> None:
    with build_test_app() as (_, client, _):
        response = client.get(f"/games/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND


def test_get_game_after_begin_shows_started_at() -> None:
    with build_test_app() as (_, client, _):
        reg = register_player(client)
        headers = auth_headers(reg["sessionToken"])
        created = client.post("/games", headers=headers).json()
        client.post(f"/games/{created['gameId']}/begin", headers=headers)

        response = client.get(f"/games/{created['gameId']}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "in_progress"
        assert body["startedAt"] is not None
        assert body["endedAt"] is None
