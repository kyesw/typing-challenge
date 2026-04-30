"""Failure-injection integration: 401s, timeouts, duplicate nicknames.

Task 14.4: drive the same FastAPI-over-httpx harness used by the
happy-path flow but poke at conditions the normal flow can't reach:

* a duplicate nickname collision — exact match and case-insensitive
  variants;
* a session token that is missing, unknown, or past its bounded
  lifetime — the three paths Requirement 7.3 treats identically on
  the wire;
* a result submission that arrives after the server-measured
  elapsed time exceeds ``Maximum_Game_Duration`` — the Game must
  transition to ``abandoned`` and the submission must be rejected.

Requirements validated:
- 1.7 — ``POST /players`` rejects case-insensitively duplicate
  nicknames with 409 ``nickname_taken``.
- 7.3 — protected endpoints (``POST /games``) reject missing,
  unknown, and expired Session_Tokens with 401 ``session_expired``.
- 9.2 — ``POST /games/{gameId}/result`` rejects a late submission
  with 409 ``game_timeout`` and the Game's final status is
  ``abandoned``.

Harness: duplicates the compact ``_async_app`` helper from
``test_full_flow.py`` (and matching the ``test_dashboard_polling``
pattern) rather than extracting it. Each integration file stays
self-contained and easy to read in isolation — the same rationale
called out in the neighbouring files.

A small extension over the happy-path harness: this suite pokes the
DB directly via the yielded ``session_factory`` to inject the
"expired session" and "started_at in the past" conditions. The
task prompt explicitly prefers that approach over
``asyncio.sleep`` because it's deterministic and fast — no
real-wall-clock waits.

Frontend-adapter redirect behaviour on 401 is already covered by
the frontend unit tests in ``frontend/src/api/client.test.ts``.
This file stays focused on the backend response contract: status
code + ``code`` in the ``ApiError`` envelope.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.main import create_app
from app.persistence import Game, Player, Prompt, PromptRepository, init_db


# ---------------------------------------------------------------------------
# Harness (duplicated from ``test_full_flow.py`` — see module docstring)
# ---------------------------------------------------------------------------


def _make_memory_engine() -> Engine:
    """Fresh in-memory SQLite engine with foreign keys enforced.

    ``StaticPool`` keeps every connection pinned to a single
    underlying SQLite database so inserts from one session are
    visible to another. ``check_same_thread=False`` lets FastAPI's
    thread-pool paths (sync DB work inside async handlers) cross
    threads safely on the shared connection.
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

    Deliberately skips FastAPI's real ``lifespan``: the lifespan would
    try to open the file-backed SQLite DB configured in
    :func:`app.config.get_settings`. We substitute our own engine,
    session factory, and PromptRepository on ``app.state`` before the
    first request lands, which is exactly what the request-scoped
    dependencies in :mod:`app.api.dependencies` read from.

    The yielded ``session_factory`` is exposed so individual tests
    can reach into the DB to inject failure conditions (expired
    session, back-dated ``started_at``) that the HTTP API does not
    have a path to produce on its own.
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
    # lifespan is not run — we bypass it entirely. Dependencies
    # only read from these attributes, so the app is ready to serve.
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
# Shared data
# ---------------------------------------------------------------------------


# A 132-character prompt, within the Prompt_Repository's [100, 500]
# validity window. The exact contents do not matter for these
# failure paths — what matters is that the Prompt_Repository has
# something to hand to ``GameService.create_game``.
PROMPT_TEXT = (
    "The quick brown fox jumps over the lazy dog, and then the lazy dog "
    "slowly blinks at the fox before rolling over for more sleep today."
)


# ---------------------------------------------------------------------------
# Duplicate nickname (Requirement 1.7)
# ---------------------------------------------------------------------------


async def test_duplicate_nickname_exact_match_returns_409() -> None:
    """Two identical nicknames in a row: first 201, second 409.

    Requirement 1.7 forbids two Active_Players from sharing a
    case-insensitively equal nickname. With identical casing the
    collision is trivially case-insensitive. The API layer surfaces
    this as 409 with ``code == 'nickname_taken'`` per
    :data:`app.errors.ErrorCode.NICKNAME_TAKEN`.

    Also asserts the ``ApiError`` envelope shape: ``code`` is stable
    machine-readable, ``message`` is human-readable, ``details``
    carries the case-folded key for diagnostics.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        first = await client.post("/players", json={"nickname": "Alice"})
        assert first.status_code == 201, first.text

        second = await client.post("/players", json={"nickname": "Alice"})
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["code"] == "nickname_taken"
        # Message is human-readable; we assert it's a non-empty
        # string rather than pinning the exact wording, which is
        # free to change.
        assert isinstance(body["message"], str) and body["message"]
        # Details carry the case-folded key the service used to
        # detect the collision — handy for ops diagnostics.
        assert body["details"] == {"nicknameCi": "alice"}


async def test_duplicate_nickname_case_insensitive_variants_return_409() -> None:
    """Register ``Alice``; every case-variant resubmission is 409.

    Requirement 1.7 treats nickname uniqueness case-insensitively.
    After the canonical ``Alice`` is taken, each of ``ALICE``,
    ``alice``, and ``aLiCe`` must collide because they all
    case-fold to the same key. Each should return 409 with
    ``code == 'nickname_taken'`` and should NOT consume the
    nickname under its own spelling.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        first = await client.post("/players", json={"nickname": "Alice"})
        assert first.status_code == 201, first.text

        for variant in ("ALICE", "alice", "aLiCe"):
            resp = await client.post(
                "/players", json={"nickname": variant}
            )
            assert resp.status_code == 409, (variant, resp.text)
            body = resp.json()
            assert body["code"] == "nickname_taken", variant
            # The case-folded collision key is stable regardless of
            # which casing the caller submitted.
            assert body["details"] == {"nicknameCi": "alice"}, variant


# ---------------------------------------------------------------------------
# Session token authorization (Requirement 7.3)
# ---------------------------------------------------------------------------


async def test_post_games_without_authorization_header_returns_401() -> None:
    """``POST /games`` with no ``Authorization`` header → 401 session_expired.

    Requirement 7.3 collapses "missing", "unknown", and "expired"
    into a single unauthorized response on the wire so an attacker
    cannot tell which failure mode fired. The service layer's
    structured ``reason`` is kept out of the HTTP response by
    :func:`app.api.dependencies.require_player`; all three paths
    return the same shape.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        resp = await client.post("/games")
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["code"] == "session_expired"
        assert isinstance(body["message"], str) and body["message"]


async def test_post_games_with_unknown_bearer_token_returns_401() -> None:
    """A garbage Bearer token that does not match any Player row → 401.

    The token does not need to be well-formed URL-safe base64 — the
    service just queries ``players.session_token == <token>`` and
    falls through to ``Unauthorized(reason='unknown')`` on a miss.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, _):
        resp = await client.post(
            "/games",
            headers={"Authorization": "Bearer definitely-not-a-real-token"},
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "session_expired"


async def test_post_games_with_expired_session_token_returns_401() -> None:
    """Register a player, back-date their session expiry, then hit ``POST /games``.

    Reaches "expired" directly by manipulating the DB rather than
    waiting: ``_async_app`` yields the session factory so we can
    set ``session_expires_at`` to a point in the past. The
    authorization check in
    :meth:`app.services.player_service.PlayerService.authorize`
    then rejects the token as expired (Requirement 7.5: strict
    inequality, a token at its expiry instant is no longer valid).

    The 401 envelope is identical to the missing/unknown cases —
    Requirement 7.3 mandates that the wire response does not
    distinguish between the three.
    """
    async with _async_app(prompt_text=PROMPT_TEXT) as (_, client, session_factory):
        reg = await client.post("/players", json={"nickname": "Alice"})
        assert reg.status_code == 201, reg.text
        token = reg.json()["sessionToken"]
        player_id = reg.json()["playerId"]

        # Sanity: the token works right now. This ensures that when
        # we assert the same call fails after the back-date, the
        # only thing that changed is ``session_expires_at``.
        create_resp = await client.post(
            "/games", headers={"Authorization": f"Bearer {token}"}
        )
        assert create_resp.status_code == 201, create_resp.text

        # Back-date the expiry by one hour. The authorize step uses
        # strict inequality, so a value strictly before ``now`` is
        # unambiguously expired.
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with session_factory() as s:
            player = s.get(Player, player_id)
            assert player is not None
            player.session_expires_at = past
            s.commit()

        # Now the token must be rejected — we can't guarantee
        # anything about the /games call's body beyond the 401 shape.
        expired_resp = await client.post(
            "/games", headers={"Authorization": f"Bearer {token}"}
        )
        assert expired_resp.status_code == 401, expired_resp.text
        body = expired_resp.json()
        assert body["code"] == "session_expired"
        assert isinstance(body["message"], str) and body["message"]


# ---------------------------------------------------------------------------
# Timeout submission (Requirement 9.2)
# ---------------------------------------------------------------------------


async def test_result_submission_after_timeout_returns_409_and_abandons_game() -> None:
    """Late ``POST /games/{id}/result`` → 409 ``game_timeout``, Game abandoned.

    Requirement 9.2 / Property 14: once the server-measured elapsed
    exceeds ``Maximum_Game_Duration``, the submission is rejected
    and the Game transitions to ``abandoned``. Subsequent fetches
    of the Game's metadata must reflect the terminal status.

    Rather than ``await asyncio.sleep``-ing past the configured
    max duration, we back-date ``games.started_at`` directly
    through the session factory. That keeps the test fast and
    deterministic: the elapsed time the server computes
    (``server_now() - started_at``) is whatever we make it, with
    no wall-clock dependency. This is the approach the task prompt
    called out as "preferred — faster, deterministic".

    We still run with a small ``max_game_duration_seconds`` so the
    back-date needed to overshoot stays minimal; this also
    exercises the real timeout branch in
    :meth:`GameService.complete`, not a corner-case with an
    absurdly large offset.
    """
    # A tiny max duration keeps the scenario realistic: back-dating
    # ``started_at`` by a handful of seconds puts us comfortably
    # past the limit without needing to skew the clock by hours.
    settings = Settings(max_game_duration_seconds=2)

    async with _async_app(
        prompt_text=PROMPT_TEXT, settings=settings
    ) as (_, client, session_factory):
        reg = (
            await client.post("/players", json={"nickname": "Alice"})
        ).json()
        headers = {"Authorization": f"Bearer {reg['sessionToken']}"}

        created = (await client.post("/games", headers=headers)).json()
        game_id = created["gameId"]

        begin = (
            await client.post(f"/games/{game_id}/begin", headers=headers)
        ).json()
        assert begin["status"] == "in_progress"

        # Back-date started_at so the server's server_now() - started_at
        # elapsed exceeds the 2-second max duration. A 60-second skew
        # is well clear of any sub-second sqlite round-trip noise.
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        with session_factory() as s:
            game = s.get(Game, game_id)
            assert game is not None
            game.started_at = past
            s.commit()

        resp = await client.post(
            f"/games/{game_id}/result",
            headers=headers,
            json={"typedText": PROMPT_TEXT},
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "game_timeout"
        # Details include the observed elapsed — enough seconds past
        # the cutoff to confirm the timeout branch fired for the
        # right reason.
        assert body["details"]["gameId"] == game_id
        assert body["details"]["elapsedSeconds"] > settings.max_game_duration_seconds

        # Requirement 9.2: the Game must now be ``abandoned``, and
        # ``GET /games/{id}`` must reflect that terminal status.
        meta = (await client.get(f"/games/{game_id}")).json()
        assert meta["status"] == "abandoned"
        # The abandon branch stamps ended_at with the server clock
        # at rejection time; the invariant ``ended_at > started_at``
        # (Requirement 8.7) must hold.
        ended_at = datetime.fromisoformat(meta["endedAt"])
        started_at = datetime.fromisoformat(meta["startedAt"])
        # Normalize both to UTC-aware so the comparison does not trip
        # on SQLite's naive round-trip of timezone-aware columns.
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        assert ended_at > started_at

        # A second submission on the now-abandoned Game must also
        # be rejected — but through the "not in_progress" path
        # rather than the timeout path. Requirement 8.5 forbids
        # transitions out of ``abandoned``; the API surfaces this
        # as 409 ``game_conflict``.
        retry = await client.post(
            f"/games/{game_id}/result",
            headers=headers,
            json={"typedText": PROMPT_TEXT},
        )
        assert retry.status_code == 409, retry.text
        retry_body = retry.json()
        assert retry_body["code"] == "game_conflict"
        assert retry_body["details"]["currentStatus"] == "abandoned"
