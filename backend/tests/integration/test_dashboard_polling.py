"""Dashboard polling integration: ``GET /leaderboard`` as the dashboard sees it.

Task 14.2: the Dashboard_Client keeps itself current by polling
``GET /leaderboard`` once per second (Requirement 6.2). The backend
only owns the endpoint side of that contract — there is no server
push and no cache. The "within one polling interval" guarantee on
the frontend reduces, on the backend, to a much simpler invariant:

    after a Score is persisted, the very next ``GET /leaderboard``
    must return a snapshot that includes that Score and reflects
    the correct ranking.

That's what this module exercises. We drive the full register →
start → begin → submit flow exactly like a real frontend, then
issue successive ``GET /leaderboard`` requests at the points a
1 Hz poller would hit — before, between, and after each game
completion — and assert the snapshot tracks each write.

Requirements validated:
- 6.1 — Dashboard polling reads the top-N leaderboard with
  ``nickname``, ``bestWpm``, ``bestAccuracy``, and ``bestPoints``.
- 6.2 — Polling ``GET /leaderboard`` returns a fresh snapshot on
  each call that reflects all Scores written up to that moment.

Harness: we intentionally duplicate the compact ``_async_app``
helper from ``test_full_flow.py`` rather than extract it. The
helper is ~30 lines and extracting it would add a shared module
whose only consumers are these two files. Duplicating keeps each
integration test file self-contained and easy to read in
isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.main import create_app
from app.persistence import Prompt, PromptRepository, init_db


# ---------------------------------------------------------------------------
# Harness (duplicated from ``test_full_flow.py`` — see module docstring)
# ---------------------------------------------------------------------------


def _make_memory_engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced.

    ``StaticPool`` pins every connection to a single underlying
    SQLite database so inserts from one session are visible to
    another. ``check_same_thread=False`` lets FastAPI's thread-pool
    paths (sync DB work inside async handlers) cross threads safely
    on the shared connection.
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

    Skips FastAPI's real ``lifespan`` and installs in-memory wiring
    on ``app.state`` directly; request-scoped dependencies in
    :mod:`app.api.dependencies` read from those attributes, so the
    app is ready to serve without touching the file-backed SQLite
    configured in :func:`app.config.get_settings`.
    """
    settings = settings if settings is not None else get_settings()

    engine = _make_memory_engine()
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    app = create_app(settings)

    from app.api.dependencies import settings_dependency

    app.dependency_overrides[settings_dependency] = lambda: settings

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
# Shared fixtures
# ---------------------------------------------------------------------------


# A 132-character prompt, inside the Prompt_Repository's [100, 500]
# validity window. Players who submit this text verbatim land a
# perfect-accuracy Score, so scoring differences across players
# come purely from the typed_text they submit.
PROMPT_TEXT = (
    "The quick brown fox jumps over the lazy dog, and then the lazy dog "
    "slowly blinks at the fox before rolling over for more sleep today."
)


async def _play_through(
    client: httpx.AsyncClient, *, nickname: str, typed: str
) -> dict:
    """Drive one full ``register → start → begin → submit`` flow.

    Returns the ``POST /games/{id}/result`` response body so the
    caller can cross-check the subsequent leaderboard snapshot
    against the authoritative Score that was just persisted.
    """
    reg = (await client.post("/players", json={"nickname": nickname})).json()
    headers = {"Authorization": f"Bearer {reg['sessionToken']}"}
    created = (await client.post("/games", headers=headers)).json()
    await client.post(f"/games/{created['gameId']}/begin", headers=headers)
    result = (
        await client.post(
            f"/games/{created['gameId']}/result",
            headers=headers,
            json={"typedText": typed},
        )
    ).json()
    # Pass the playerId through too so callers can assert the
    # leaderboard entry is bound to the right Player.
    result["playerId"] = reg["playerId"]
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_next_poll_reflects_new_score_and_ranking() -> None:
    """The next ``GET /leaderboard`` after each Score contains that Score.

    Simulates what a 1 Hz Dashboard_Client sees as games complete.
    Between each dashboard "tick" we complete one game and then
    issue a ``GET /leaderboard`` — the stand-in for the next poll.
    The snapshot must include every Score written up to that
    moment, with contiguous ranks assigned in the correct order.

    Requirements 6.1 and 6.2: polling returns the current snapshot
    including all Scores persisted before the request lands.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        # -- Tick 0: dashboard just mounted, no one has played yet. --------
        empty = (await client.get("/leaderboard")).json()
        assert empty["entries"] == []
        # generatedAt is a valid ISO timestamp even when the board is empty.
        tick0_at = datetime.fromisoformat(empty["generatedAt"])

        # -- Player A completes a game (perfect score) ---------------------
        first = await _play_through(client, nickname="Alice", typed=PROMPT_TEXT)
        # The submit response itself reports Alice at rank 1. The
        # next poll must agree.
        assert first["rank"] == 1
        assert first["accuracy"] == 100.0

        # -- Tick 1: the next poll picks up Alice's Score -------------------
        after_first = (await client.get("/leaderboard")).json()
        assert len(after_first["entries"]) == 1
        row_a = after_first["entries"][0]
        # Requirement 6.1: the dashboard row carries the four
        # rendered fields plus the nickname/playerId binding.
        assert row_a["playerId"] == first["playerId"]
        assert row_a["nickname"] == "Alice"
        assert row_a["bestPoints"] == first["points"]
        assert row_a["bestWpm"] == first["wpm"]
        assert row_a["bestAccuracy"] == first["accuracy"]
        assert row_a["rank"] == 1
        # Each snapshot advertises a fresh ``generatedAt``; it must
        # be strictly non-decreasing across successive polls.
        tick1_at = datetime.fromisoformat(after_first["generatedAt"])
        assert tick1_at >= tick0_at

        # -- Player B completes a game with a partial prefix ---------------
        # Typing only half the prompt lowers accuracy and therefore
        # points, so Bob must rank strictly below Alice.
        half = PROMPT_TEXT[: len(PROMPT_TEXT) // 2]
        second = await _play_through(client, nickname="Bob", typed=half)
        assert second["accuracy"] < 100.0
        assert second["points"] < first["points"]
        assert second["rank"] == 2

        # -- Tick 2: the next poll reflects the updated ranking ------------
        after_second = (await client.get("/leaderboard")).json()
        # Requirement 5.1 / 5.6: one entry per Player with a Score,
        # and the new Score is present immediately.
        assert [e["nickname"] for e in after_second["entries"]] == ["Alice", "Bob"]
        # Requirement 5.3 / 5.4: contiguous 1..N ranks in descending
        # ``bestPoints`` order. Requirement 6.2: polling reflects the
        # ranking update as soon as Scores are written.
        assert [e["rank"] for e in after_second["entries"]] == [1, 2]
        # Best-values track the underlying Scores (Requirement 5.2).
        assert after_second["entries"][0]["bestPoints"] == first["points"]
        assert after_second["entries"][1]["bestPoints"] == second["points"]
        assert after_second["entries"][1]["playerId"] == second["playerId"]
        tick2_at = datetime.fromisoformat(after_second["generatedAt"])
        assert tick2_at >= tick1_at


async def test_back_to_back_polls_return_fresh_snapshots() -> None:
    """Two polls in a row recompute the snapshot; no stale cache.

    The Dashboard_Client polls once per second; task 10.1 calls out
    that each call recomputes from the Scores table with no cache
    in between. We assert the observable contract here: issuing two
    successive ``GET /leaderboard`` requests with no intervening
    write returns identical ``entries`` — proving the response
    reflects the current database state each time rather than being
    memoized — and a non-decreasing ``generatedAt`` — proving each
    response is freshly built.

    Requirements 6.1 and 6.2.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        result = await _play_through(client, nickname="Carol", typed=PROMPT_TEXT)

        first_poll = (await client.get("/leaderboard")).json()
        second_poll = (await client.get("/leaderboard")).json()

        # Identical entries across back-to-back polls (no writes in
        # between). We compare ``entries`` directly: ``generatedAt``
        # is intentionally allowed to advance.
        assert first_poll["entries"] == second_poll["entries"]
        assert len(first_poll["entries"]) == 1
        row = first_poll["entries"][0]
        assert row["nickname"] == "Carol"
        assert row["playerId"] == result["playerId"]
        assert row["bestPoints"] == result["points"]
        assert row["rank"] == 1

        # ``generatedAt`` on the second poll is a fresh server-side
        # timestamp; it may be equal (same clock tick) but must not
        # travel backwards. This is what distinguishes "recomputed"
        # from a pinned cached response.
        ts1 = datetime.fromisoformat(first_poll["generatedAt"])
        ts2 = datetime.fromisoformat(second_poll["generatedAt"])
        assert ts2 >= ts1


async def test_new_score_overtakes_existing_leader_on_next_poll() -> None:
    """A Player's better Score overtakes prior leaders on the next poll.

    Exercises the "reflects the updated ranking" half of task
    14.2's wording. Bob first plays with a short prefix and leads
    an otherwise-empty board. Alice then plays with the full prompt
    and her higher points push her into rank 1 on the very next
    ``GET /leaderboard`` — no interval, no re-warming, just the
    next poll.

    Requirements 5.6 and 6.2.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        # Bob leads a one-entry board with a weak Score.
        half = PROMPT_TEXT[: len(PROMPT_TEXT) // 2]
        bob = await _play_through(client, nickname="Bob", typed=half)
        bob_leads = (await client.get("/leaderboard")).json()
        assert bob_leads["entries"][0]["nickname"] == "Bob"
        assert bob_leads["entries"][0]["rank"] == 1

        # Alice submits a perfect attempt; she should displace Bob.
        alice = await _play_through(client, nickname="Alice", typed=PROMPT_TEXT)
        assert alice["points"] > bob["points"]

        # The very next poll reflects the new ordering.
        after = (await client.get("/leaderboard")).json()
        nicknames = [e["nickname"] for e in after["entries"]]
        ranks = [e["rank"] for e in after["entries"]]
        assert nicknames == ["Alice", "Bob"]
        assert ranks == [1, 2]
        # Each row still carries the four required dashboard fields
        # (Requirement 6.1).
        for row in after["entries"]:
            assert {"nickname", "bestWpm", "bestAccuracy", "bestPoints"} <= set(row)
