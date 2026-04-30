"""Concurrency integration: K players drive the full flow in parallel.

Task 14.3: fire K players through ``register → start → begin →
submit`` concurrently against a single FastAPI app/DB instance and
assert the resulting ``GET /leaderboard`` snapshot is internally
consistent — exactly one entry per player, ranks contiguous over
``1..K``, ordering by ``bestPoints`` descending with the documented
tie-breakers, and each player's ``bestPoints`` equals the points
reported when their Score was submitted.

Requirements validated:
- 5.1 — one LeaderboardEntry per Player with a completed Score
  (exactly K entries; no duplicate ``playerId``).
- 5.2 — per-player ``bestPoints`` / ``bestWpm`` / ``bestAccuracy``
  equal the corresponding submitted values (each player submitted
  exactly once, so the "max across scores" aggregation degenerates
  to the single submitted Score).
- 5.3 — entries are ordered by ``bestPoints`` descending; ties
  broken by ``bestWpm`` descending (the earlier-``createdAt``
  tiebreak is not directly observable in the response but is
  consistent with a total order on the exposed fields).
- 5.4 — ``rank`` forms the contiguous sequence ``1..K`` in order.

Harness: the ``_async_app`` helper mirrors the one used by
``test_full_flow.py`` / ``test_dashboard_polling.py`` rather than
being extracted. The helper is small and extracting it would add
a shared module whose only consumers are these integration files;
duplicating keeps each file self-contained and easy to read in
isolation — the same rationale called out in
``test_dashboard_polling.py``. The one divergence from the other
two files is the engine: we use a per-test temp file SQLite DB
instead of ``:memory:`` + ``StaticPool`` (see the note below).

Note on SQLite concurrency: the in-memory ``StaticPool`` harness
used by ``test_full_flow.py`` / ``test_dashboard_polling.py``
pins every session to a single sqlite3 connection, which fails
when K async handlers run on different threadpool workers at the
same instant ("bad parameter or other API misuse" on the shared
cursor). This test therefore uses a per-test temp file SQLite
database so SQLAlchemy's default ``QueuePool`` can hand out a
real per-thread connection. The on-disk DB is still private to
one test run and is deleted on teardown.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.main import create_app
from app.persistence import Prompt, PromptRepository, init_db


# ---------------------------------------------------------------------------
# Harness (mirrors ``test_full_flow.py`` with a file-backed engine — see
# the module docstring for why this file diverges from the in-memory
# ``:memory:`` + ``StaticPool`` harness used elsewhere.)
# ---------------------------------------------------------------------------


def _make_engine() -> tuple[Engine, str]:
    """Fresh file-backed SQLite engine with foreign keys enforced.

    Concurrency note: this test fires K players through the full
    flow in parallel, so the FastAPI threadpool will end up
    executing multiple sync DB handlers on different threads at the
    same instant. ``sqlite:///:memory:`` with ``StaticPool`` pins
    every session to a single underlying sqlite3 connection, and
    sqlite3's C library raises ``InterfaceError: bad parameter or
    other API misuse`` when two threads try to drive the same
    connection simultaneously — the "cursor is still in use"
    failure mode.

    Using a temp file database side-steps that: SQLAlchemy's
    default ``QueuePool`` now has real per-thread connections to
    hand out, each thread gets its own sqlite3 connection, and the
    on-disk DB stays shared across them. This mirrors the v1
    single-host deployment (file-backed SQLite) more faithfully
    than the in-memory harness used elsewhere in this suite, which
    is exactly what task 14.3 wants to exercise.

    Returns the engine plus the path to the temp file so the caller
    can remove it on teardown.
    """
    tmp_path = os.path.join(
        tempfile.gettempdir(), f"typing-game-concurrency-{uuid.uuid4().hex}.db"
    )
    engine = create_engine(
        f"sqlite:///{tmp_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine, tmp_path


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
    """Yield ``(app, async_client, session_factory)`` wired to a per-test SQLite DB.

    Skips FastAPI's real ``lifespan`` and installs the test-local
    engine, session factory, and prompt repository on ``app.state``
    directly; request-scoped dependencies in
    :mod:`app.api.dependencies` read from those attributes, so the
    app is ready to serve without touching the production-default
    SQLite URL configured in :func:`app.config.get_settings`.
    """
    settings = settings if settings is not None else get_settings()

    engine, db_path = _make_engine()
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
            engine.dispose()
            # Best-effort cleanup of the temp DB file. ``engine.dispose``
            # releases the sqlite3 connection, after which the file
            # can be removed.
            try:
                os.remove(db_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Shared data
# ---------------------------------------------------------------------------


# A 132-character prompt, within the Prompt_Repository's
# [100, 500] validity window. Players submit progressively shorter
# prefixes so each Score lands a deterministically different
# ``correct_chars`` — the resulting ``accuracy`` values are strictly
# monotonic in prefix length, which makes the per-player assertions
# below independent of any run-to-run timing jitter.
PROMPT_TEXT = (
    "The quick brown fox jumps over the lazy dog, and then the lazy dog "
    "slowly blinks at the fox before rolling over for more sleep today."
)


# K = 10 players is enough to exercise the aggregation and ordering
# logic without bumping into the default per-IP rate limit on
# ``POST /players`` (10/min). All concurrent requests share the
# same ASGI-attributed source IP, so K must stay at-or-below that
# bucket's capacity to keep the test deterministic.
K_PLAYERS = 10


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_concurrent_players_produce_consistent_leaderboard() -> None:
    """K players finish concurrently; the leaderboard is consistent.

    Fires ``K_PLAYERS`` full flows with :func:`asyncio.gather` so
    registration, game creation, begin, and result submission
    interleave through FastAPI's async scheduler against a single
    shared SQLite database. After every flow has landed, we inspect
    ``GET /leaderboard`` and assert:

    * exactly K entries, one per registered player (Requirement 5.1);
    * each ``playerId`` appears exactly once (Requirement 5.1);
    * ranks form the contiguous sequence ``1..K`` (Requirement 5.4);
    * entries are ordered by ``bestPoints`` descending with
      ``bestWpm`` descending as the first visible tiebreak
      (Requirement 5.3);
    * each player's ``bestPoints`` / ``bestWpm`` / ``bestAccuracy``
      on the leaderboard equal the values returned by their own
      ``POST /games/{id}/result`` response (Requirement 5.2 —
      "max across Scores" degenerates to "the single submitted
      Score" when each player submits exactly once).
    """

    async def _play_through(
        client: httpx.AsyncClient, *, nickname: str, typed: str
    ) -> dict:
        """Drive a single ``register → start → begin → submit`` flow.

        Each HTTP call is awaited before the next; the concurrency
        happens at the *player* level via :func:`asyncio.gather`
        below, not by pipelining within a single player's flow.
        That matches the real frontend's behavior — a player can
        only be on one page at a time.
        """
        reg = (
            await client.post("/players", json={"nickname": nickname})
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
                json={"typedText": typed},
            )
        ).json()
        # Carry the playerId + nickname through so the caller can
        # cross-check the leaderboard entry against the Score that
        # was actually persisted for this player.
        result["playerId"] = reg["playerId"]
        result["nickname"] = nickname
        return result

    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        # Each index ``i`` types a prefix that is ``step * i``
        # characters shorter than the full prompt. With
        # ``step == 10`` and K == 10 the prefix lengths span 132
        # down to 42, every value distinct and every one producing
        # a strictly different ``correct_chars`` count — which in
        # turn yields a strictly monotonic ``accuracy`` per player.
        step = 10

        async def _player_flow(i: int) -> dict:
            nickname = f"Player-{i:02d}"
            # Player 0 types the full prompt (perfect submission);
            # later players type progressively shorter prefixes.
            typed = PROMPT_TEXT[: len(PROMPT_TEXT) - i * step]
            return await _play_through(
                client, nickname=nickname, typed=typed
            )

        # Run all K flows concurrently. ``asyncio.gather`` schedules
        # them together on the event loop; FastAPI's async handlers
        # can interleave at every ``await`` point, which is where
        # DB work would contend in a real multi-worker deployment.
        submitted = await asyncio.gather(
            *(_player_flow(i) for i in range(K_PLAYERS))
        )

        # Sanity check on what came back from ``/result``: exactly
        # K responses, each with a non-empty playerId. If gather
        # dropped or duplicated a response we'd catch it here
        # before the leaderboard comparison.
        assert len(submitted) == K_PLAYERS
        assert len({r["playerId"] for r in submitted}) == K_PLAYERS

        # ---- GET /leaderboard --------------------------------------------
        lb = (await client.get("/leaderboard")).json()
        entries = lb["entries"]

        # Requirement 5.1: one entry per Player with a Score.
        assert len(entries) == K_PLAYERS
        # No duplicate ``playerId`` rows — each registered player
        # appears exactly once in the snapshot.
        lb_player_ids = [e["playerId"] for e in entries]
        assert len(set(lb_player_ids)) == K_PLAYERS
        # And the set of leaderboard playerIds matches the set of
        # playerIds returned by the K submissions (no extras, no
        # missing rows).
        assert set(lb_player_ids) == {r["playerId"] for r in submitted}

        # Requirement 5.4: contiguous 1..K ranks.
        ranks = [e["rank"] for e in entries]
        assert ranks == list(range(1, K_PLAYERS + 1))

        # Requirement 5.3: entries are ordered by ``bestPoints``
        # descending with ``bestWpm`` descending as the visible
        # tiebreak. The earliest-``createdAt`` tiebreak is not
        # directly observable in the response, so we validate the
        # ordering lexicographically on the two exposed keys.
        order_keys = [(-e["bestPoints"], -e["bestWpm"]) for e in entries]
        assert order_keys == sorted(order_keys), (
            "Leaderboard entries are not ordered by (bestPoints desc, "
            f"bestWpm desc); got {order_keys}"
        )

        # Requirement 5.2: per-player bests equal the single Score
        # each player submitted. Build a ``playerId -> entry`` map
        # and a ``playerId -> submitted`` map for a clean comparison.
        by_player_lb = {e["playerId"]: e for e in entries}
        by_player_sub = {r["playerId"]: r for r in submitted}

        for pid, sub in by_player_sub.items():
            entry = by_player_lb[pid]
            # Nickname is carried through from registration onto the
            # leaderboard row, so a mislinked Player/Game would show
            # up here.
            assert entry["nickname"] == sub["nickname"]
            # Each player submitted exactly one Score, so the
            # "max across that player's Scores" aggregation reduces
            # to equality with the submitted Score.
            assert entry["bestPoints"] == sub["points"]
            assert entry["bestWpm"] == sub["wpm"]
            assert entry["bestAccuracy"] == sub["accuracy"]
            # NB: we intentionally do *not* assert that the rank
            # each player received in their ``/result`` response
            # equals their final rank here. The rank on the submit
            # response is a point-in-time snapshot reflecting only
            # Scores persisted before that submission landed, so
            # when submissions interleave concurrently a player
            # who submits early can see rank 1 and then get
            # displaced once a later, higher-scoring submission
            # commits. The authoritative ranks are the ones on the
            # aggregated ``/leaderboard`` snapshot, which we've
            # already validated above as contiguous ``1..K`` in
            # descending-``bestPoints`` order.
