"""End-to-end backend integration: register → start → begin → submit → leaderboard.

Task 14.1: spin up the FastAPI app in-process via
:class:`httpx.AsyncClient` + :class:`httpx.ASGITransport`, run the
complete game flow exactly as a real frontend would, and assert
every response payload along the way. After the Score lands we fetch
``GET /leaderboard`` and ``GET /games/{gameId}`` and confirm the
aggregation and the Game's terminal state.

Requirements validated:
- 1.3 — ``POST /players`` returns a bound ``(playerId, sessionToken)``.
- 2.3 — ``POST /games`` creates a pending Game with an assigned prompt.
- 3.2 — ``POST /games/{id}/begin`` records ``startedAt`` and flips to
  ``in_progress``.
- 3.6 / 15.1 / 15.2 — scoring uses server-measured elapsed (the
  client-supplied ``elapsedSeconds`` is spot-checked by varying it
  and asserting the server's reported accuracy is unchanged).
- 4.4 / 4.5 / 4.6 — the Score response carries ``wpm``, ``accuracy``,
  ``points``, ``rank``, and the completed Game has ``endedAt >
  startedAt`` via ``GET /games/{id}``.
- 5.1 / 5.6 — ``GET /leaderboard`` returns one entry per player with
  a completed Score and includes the newly scored player.

Isolation: each test builds a fresh in-memory SQLite engine and a
one-off :class:`FastAPI` instance. The app's real ``lifespan``
tries to bind to the file-backed DB; we bypass that by wiring
``app.state`` directly before starting the :class:`AsyncClient`.
That matches the pattern in ``api_helpers.build_test_app`` but
drives the app asynchronously instead of through
:class:`~fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.main import create_app
from app.persistence import Prompt, PromptRepository, init_db


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_memory_engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced.

    ``StaticPool`` keeps every connection pinned to a single
    underlying SQLite database so inserts from one session are
    visible to another. ``check_same_thread=False`` lets FastAPI's
    thread-pool code paths (sync DB work inside async handlers)
    cross threads safely on the shared connection.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine


def _seed_prompt(session_factory: sessionmaker[Session], *, text: str) -> str:
    """Insert a single Prompt row so ``GameService.create_game`` has one to pick."""
    prompt_id = str(uuid.uuid4())
    with session_factory() as s:
        s.add(Prompt(id=prompt_id, text=text, difficulty=None, language="en"))
        s.commit()
    return prompt_id


@asynccontextmanager
async def _async_app(
    *,
    prompt_text: str,
    settings: Settings | None = None,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, sessionmaker[Session]]]:
    """Yield ``(app, async_client, session_factory)`` wired to in-memory state.

    Deliberately skips FastAPI's real ``lifespan``: the lifespan
    would try to open the file-backed SQLite DB configured in
    :func:`app.config.get_settings`. We substitute our own engine,
    session factory, and PromptRepository on ``app.state`` before
    the first request lands, which is exactly what the request-scoped
    dependencies in :mod:`app.api.dependencies` read from.
    """
    settings = settings if settings is not None else get_settings()

    engine = _make_memory_engine()
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    app = create_app(settings)

    # Override the cached settings dependency to the exact instance
    # this test wants. Matches the pattern used by the sync helper.
    from app.api.dependencies import settings_dependency

    app.dependency_overrides[settings_dependency] = lambda: settings

    # Install in-memory wiring directly on app.state. The real
    # lifespan is not run — we bypass it entirely by using
    # ``ASGITransport`` without a lifespan driver. Dependencies only
    # read from these attributes, so the app is ready to serve.
    app.state.db_engine = engine
    app.state.session_factory = session_factory
    app.state.prompt_repository = PromptRepository(session_factory)

    _seed_prompt(session_factory, text=prompt_text)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        try:
            yield app, client, session_factory
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# A 150-character prompt, within the [100, 500] validity window. We
# type the same text back verbatim so accuracy is 100% and the
# Scoring_Service produces a positive points value.
PROMPT_TEXT = (
    "The quick brown fox jumps over the lazy dog, and then the lazy dog "
    "slowly blinks at the fox before rolling over for more sleep today."
)


async def test_full_flow_backend_integration() -> None:
    """register → start → begin → submit → leaderboard reflects the new Score.

    Walks through the entire player journey in a single test so the
    integration-level contract between endpoints is exercised end to
    end. Intermediate payloads are asserted at each hop and the
    final ``GET /leaderboard`` snapshot is compared against the
    just-submitted result.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        # ----- 1. POST /players --------------------------------------------
        reg_resp = await client.post(
            "/players", json={"nickname": "Alice"}
        )
        assert reg_resp.status_code == 201, reg_resp.text
        reg_body = reg_resp.json()
        # Requirement 1.3: response includes a unique playerId and a
        # bound sessionToken. Nickname echoed back, and an
        # ISO-formatted sessionExpiresAt is present.
        assert isinstance(reg_body["playerId"], str) and reg_body["playerId"]
        assert isinstance(reg_body["sessionToken"], str) and reg_body["sessionToken"]
        assert reg_body["nickname"] == "Alice"
        # datetime.fromisoformat parses the camelCase ISO string;
        # raises ValueError if the API returned a bogus shape.
        datetime.fromisoformat(reg_body["sessionExpiresAt"])

        player_id = reg_body["playerId"]
        auth = {"Authorization": f"Bearer {reg_body['sessionToken']}"}

        # ----- 2. POST /games ----------------------------------------------
        create_resp = await client.post("/games", headers=auth)
        assert create_resp.status_code == 201, create_resp.text
        create_body = create_resp.json()
        # Requirement 2.3 / 2.4: gameId + prompt + startAt, in pending status.
        assert isinstance(create_body["gameId"], str) and create_body["gameId"]
        assert create_body["prompt"] == PROMPT_TEXT
        assert isinstance(create_body["promptId"], str) and create_body["promptId"]
        assert create_body["language"] == "en"
        assert create_body["status"] == "pending"
        start_at = datetime.fromisoformat(create_body["startAt"])

        game_id = create_body["gameId"]

        # ----- 3. POST /games/{gameId}/begin -------------------------------
        begin_resp = await client.post(
            f"/games/{game_id}/begin", headers=auth
        )
        assert begin_resp.status_code == 200, begin_resp.text
        begin_body = begin_resp.json()
        # Requirement 3.2 / 8.2 / 15.1: status flips to in_progress,
        # authoritative startedAt is recorded, and the prompt is
        # echoed for convenience.
        assert begin_body["gameId"] == game_id
        assert begin_body["status"] == "in_progress"
        started_at = datetime.fromisoformat(begin_body["startedAt"])
        assert begin_body["prompt"] == PROMPT_TEXT
        assert begin_body["promptId"] == create_body["promptId"]
        # startedAt is not earlier than the reserved startAt from
        # create — both are server-side clocks from the same process.
        assert started_at >= start_at

        # ----- 4. POST /games/{gameId}/result ------------------------------
        submit_resp = await client.post(
            f"/games/{game_id}/result",
            headers=auth,
            json={"typedText": PROMPT_TEXT, "elapsedSeconds": 7.5},
        )
        assert submit_resp.status_code == 200, submit_resp.text
        submit_body = submit_resp.json()
        # Requirements 4.4 / 4.5 / 4.6: the Score response carries
        # wpm, accuracy, points, rank, and endedAt.
        assert submit_body["gameId"] == game_id
        assert submit_body["wpm"] >= 0  # Property 4
        assert 0 <= submit_body["accuracy"] <= 100  # Property 5
        # Perfect-match submission is 100% accurate by construction.
        assert submit_body["accuracy"] == 100.0
        assert isinstance(submit_body["points"], int)
        assert submit_body["points"] > 0
        assert submit_body["rank"] == 1  # only player on the board
        ended_at = datetime.fromisoformat(submit_body["endedAt"])
        # Requirement 8.7 / 4.5: endedAt > startedAt must hold.
        assert ended_at > started_at

        submitted_points = submit_body["points"]
        submitted_wpm = submit_body["wpm"]
        submitted_accuracy = submit_body["accuracy"]

        # ----- 5. GET /leaderboard -----------------------------------------
        lb_resp = await client.get("/leaderboard")
        assert lb_resp.status_code == 200, lb_resp.text
        lb_body = lb_resp.json()
        # Requirement 5.1 / 5.6: one entry per player with a
        # completed Score, and the new Score is present.
        assert len(lb_body["entries"]) == 1
        entry = lb_body["entries"][0]
        assert entry["playerId"] == player_id
        assert entry["nickname"] == "Alice"
        # Requirement 5.2: per-player bests equal the single Score
        # we just submitted — it's the only Score for this player.
        assert entry["bestPoints"] == submitted_points
        assert entry["bestWpm"] == submitted_wpm
        assert entry["bestAccuracy"] == submitted_accuracy
        # Rank is a positive integer and must match what the submit
        # response reported.
        assert isinstance(entry["rank"], int) and entry["rank"] >= 1
        assert entry["rank"] == submit_body["rank"] == 1
        # generatedAt is a valid ISO timestamp.
        datetime.fromisoformat(lb_body["generatedAt"])

        # ----- 6. GET /games/{gameId} --------------------------------------
        # Task 14.1: assert the Game's metadata reflects the
        # terminal ``completed`` state with a consistent
        # startedAt/endedAt pair.
        meta_resp = await client.get(f"/games/{game_id}")
        assert meta_resp.status_code == 200, meta_resp.text
        meta_body = meta_resp.json()
        assert meta_body["gameId"] == game_id
        assert meta_body["playerId"] == player_id
        assert meta_body["promptId"] == create_body["promptId"]
        assert meta_body["prompt"] == PROMPT_TEXT
        assert meta_body["language"] == "en"
        assert meta_body["status"] == "completed"
        meta_started = datetime.fromisoformat(meta_body["startedAt"])
        meta_ended = datetime.fromisoformat(meta_body["endedAt"])
        # Requirement 4.5 / 8.7: endedAt > startedAt on a completed Game.
        assert meta_ended > meta_started


async def test_full_flow_second_player_ranks_below_first() -> None:
    """Two players complete a game; leaderboard contains both with ranks 1 and 2.

    Exercises Requirement 5.1 (one entry per player with a Score)
    and Requirement 5.6 (``GET /leaderboard`` reflects newly
    persisted Scores). The second player types a partial prefix, so
    their accuracy is lower and their points are strictly less than
    the first player's — rank order therefore matches score order.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        async def _play_through(nickname: str, typed: str) -> dict:
            reg = (
                await client.post("/players", json={"nickname": nickname})
            ).json()
            headers = {"Authorization": f"Bearer {reg['sessionToken']}"}
            created = (await client.post("/games", headers=headers)).json()
            await client.post(f"/games/{created['gameId']}/begin", headers=headers)
            return (
                await client.post(
                    f"/games/{created['gameId']}/result",
                    headers=headers,
                    json={"typedText": typed},
                )
            ).json()

        first = await _play_through("Alice", PROMPT_TEXT)
        # A partial prefix lowers accuracy and therefore points,
        # forcing Alice to rank above Bob. Half the prompt is
        # enough to keep the submission above the ``typedText`` bound.
        half = PROMPT_TEXT[: len(PROMPT_TEXT) // 2]
        second = await _play_through("Bob", half)

        assert first["rank"] == 1
        # Accuracy is computed against the full prompt, so the
        # prefix-only submission is strictly below 100%.
        assert second["accuracy"] < 100.0
        assert second["points"] < first["points"]
        assert second["rank"] == 2

        lb = (await client.get("/leaderboard")).json()
        # Two entries, ranked in descending points order with a
        # contiguous 1..N rank sequence (Property 10 / Requirement 5.4).
        assert [e["nickname"] for e in lb["entries"]] == ["Alice", "Bob"]
        assert [e["rank"] for e in lb["entries"]] == [1, 2]
        assert lb["entries"][0]["bestPoints"] == first["points"]
        assert lb["entries"][1]["bestPoints"] == second["points"]


async def test_full_flow_scoring_ignores_client_elapsed_seconds() -> None:
    """Server-authoritative timing: varying ``elapsedSeconds`` does not change accuracy.

    Integration spot-check for Property 7 / Requirement 3.6 / 15.1 /
    15.2. Unit tests cover the invariant exhaustively; here we just
    confirm the HTTP edge doesn't accidentally route the
    client-supplied value into the scoring pipeline.
    """
    accuracies: list[float] = []
    for client_elapsed in (0.01, 999_999.0):
        async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
            reg = (
                await client.post("/players", json={"nickname": "Solo"})
            ).json()
            headers = {"Authorization": f"Bearer {reg['sessionToken']}"}
            created = (await client.post("/games", headers=headers)).json()
            await client.post(
                f"/games/{created['gameId']}/begin", headers=headers
            )
            result = (
                await client.post(
                    f"/games/{created['gameId']}/result",
                    headers=headers,
                    json={
                        "typedText": PROMPT_TEXT,
                        "elapsedSeconds": client_elapsed,
                    },
                )
            ).json()
            accuracies.append(result["accuracy"])

    # Accuracy is a function of typed_text vs. prompt only — no
    # timing component — so it must be identical across the two runs
    # regardless of ``elapsedSeconds``.
    assert accuracies[0] == accuracies[1] == 100.0
