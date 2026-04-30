"""Property-based test for rate limiting without side effects (task 9.2).

**Property 19: Rate-limited endpoints reject over-limit requests without
side effects.**

**Validates: Requirements 14.1, 14.2, 14.3.**

For any sequence of requests to ``POST /players`` or ``POST /games``
that exceeds the configured rate limit, the Backend_API SHALL respond
with a rate-limit-exceeded response (HTTP 429 with the shared
:class:`app.errors.ApiError` envelope) for the over-limit requests and
SHALL NOT create a Player or Game record as a result of those requests.

What the property actually asserts:

1. **POST /players (per IP, Requirement 14.1).** Given capacity ``C``
   and a sequence of ``N`` otherwise-valid registration requests from
   a single IP, the first ``min(N, C)`` succeed (201) and the
   remaining ``max(0, N - C)`` are rejected with 429
   ``rate_limited``. The number of ``players`` rows persisted equals
   the number of 2xx responses.
2. **POST /games per IP (Requirement 14.2, IP scope).** Given N
   already-registered players sharing a single IP and a per-IP
   capacity of ``C_ip``, and with a per-player capacity larger than
   ``C_ip`` (so the IP bucket is what fires first), only ``C_ip``
   games are created. Over-limit games return 429 and no Game row
   is persisted.
3. **POST /games per player (Requirement 14.2, player scope).**
   Given a single authenticated player and a per-player capacity of
   ``C_pl`` (smaller than the per-IP capacity), the first ``C_pl``
   game-creation requests succeed, then further requests return 429
   — and because the first success transitions the player's Game to
   ``pending`` status (blocking a second create via the domain rule
   in Requirement 2.6 / Property 13), later successes on the same
   player collapse to 409 ``game_conflict``. The rate limiter runs
   **before** the service layer (Requirement 14.3 / Property 19), so
   the 429 boundary is independent of the 409 boundary: we count
   them separately and assert the 429 count matches the over-limit
   arithmetic.
4. **No side effects on 429.** Across all three scenarios, the count
   of persisted rows (``players`` for scenario 1, ``games`` for
   scenarios 2 and 3) equals the count of non-429 responses,
   confirming Property 19's "SHALL NOT create" clause.

Strategy notes:

- Each ``@given`` example builds a fresh FastAPI app wired against an
  in-memory SQLite engine. This gives each example empty buckets and
  an empty DB.
- The rate limits are pinned to small values via a custom
  :class:`~app.config.Settings` so Hypothesis can shrink sequence
  lengths effectively. The token bucket refills continuously in wall
  time; per-minute rates of 4-6 mean the refill interval is
  ~10-15 seconds, well longer than any test example, so no refill
  happens mid-example and the capacity equals the starting inventory
  (the whole bucket) for the whole test run.
- The TestClient sends all requests from the same ASGI-attributed
  IP (``testclient``), which the :func:`~app.api.dependencies._source_ip`
  helper reads from ``request.client.host``. That's what we want: the
  property is specifically about "same source" over-limit behavior.

Out of scope:
- Refill behavior across wall-clock time — tested in the unit tests
  of :class:`TokenBucketLimiter` with an injected clock.
- The 409 nickname-uniqueness path on ``POST /players`` — avoided by
  generating distinct nicknames per request.
"""

from __future__ import annotations

import string
import uuid

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from sqlalchemy import select

from app.config import Settings
from app.errors import ErrorCode
from app.persistence.models import Game, Player

from api_helpers import auth_headers, build_test_app, register_player


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------
#
# Each example builds a fresh FastAPI app + in-memory SQLite engine,
# which is not cheap. ``deadline=None`` lets slow CI machines run the
# examples without per-case deadline flakes. We keep ``max_examples``
# modest (25) — the property is a fairly narrow predicate over
# sequence length and capacity, so a handful of shrinks is enough to
# find counter-examples.

try:
    settings.register_profile(
        "rate-limit-no-side-effects",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    pass

settings.load_profile("rate-limit-no-side-effects")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate a small per-minute rate limit. Staying in ``[2, 6]`` keeps
# the test quick while still exercising both the pre-limit-allowed and
# post-limit-rejected regions for modest sequence sizes.
_small_rate = st.integers(min_value=2, max_value=6)

# Number of requests to send in a single example. We pick up to 2x the
# capacity so the over-limit region always exists (when ``count > cap``)
# while the lower end still covers the "strictly within limit" case.
_request_count = st.integers(min_value=1, max_value=12)


@st.composite
def _rate_and_count(
    draw: st.DrawFn, max_count_factor: int = 3
) -> tuple[int, int]:
    """Draw a ``(rate_limit, request_count)`` pair.

    ``request_count`` is drawn from ``[1, rate_limit * max_count_factor]``
    so every example has a non-trivial chance of crossing the limit
    while still shrinking toward small values.
    """
    rate = draw(_small_rate)
    count = draw(st.integers(min_value=1, max_value=rate * max_count_factor))
    return rate, count


# Generate a nickname that passes format validation (Requirement 1.5 /
# 1.6). We pad with a uuid suffix to guarantee uniqueness across a
# single example's sequence so no 409 ``nickname_taken`` interferes.
_VALID_CHARS = string.ascii_letters + string.digits + "_-"


def _unique_nickname(prefix: str = "p") -> str:
    """Return a unique nickname that satisfies Requirements 1.5 / 1.6.

    ``uuid4().hex[:8]`` is 8 lowercase hex chars; combined with a
    single-letter prefix we stay well inside ``[2, 20]`` and only
    emit characters from ``[A-Za-z0-9]``.
    """
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _players_count(session_factory) -> int:
    """Return the number of rows in the ``players`` table."""
    with session_factory() as s:
        rows = s.execute(select(Player.id)).scalars().all()
    return len(rows)


def _games_count(session_factory) -> int:
    """Return the number of rows in the ``games`` table."""
    with session_factory() as s:
        rows = s.execute(select(Game.id)).scalars().all()
    return len(rows)


# ---------------------------------------------------------------------------
# Test 1 — POST /players per IP (Requirement 14.1)
# ---------------------------------------------------------------------------


@given(pair=_rate_and_count())
@settings(max_examples=25, deadline=None)
def test_players_rate_limit_rejects_over_limit_without_side_effects(
    pair: tuple[int, int],
) -> None:
    """Property 19 for ``POST /players``.

    For capacity ``C`` and ``N`` registration requests from a single
    IP with distinct, valid nicknames:

    * The first ``min(N, C)`` return 201; any further requests return
      429 ``rate_limited``.
    * The ``players`` table ends with exactly ``min(N, C)`` rows — no
      rejected request created a Player (Requirement 14.3 /
      Property 19's "SHALL NOT create" clause).
    """
    rate, count = pair

    # Pin only the players-per-IP cap. We leave the other rate limits
    # at their defaults so this test does not accidentally couple to
    # them.
    test_settings = Settings(
        rate_limit_players_per_ip_per_minute=rate,
    )

    with build_test_app(settings=test_settings) as (_, client, session_factory):
        status_counts: dict[int, int] = {}
        error_codes: list[str] = []
        for _ in range(count):
            response = client.post(
                "/players",
                json={"nickname": _unique_nickname()},
            )
            status_counts[response.status_code] = (
                status_counts.get(response.status_code, 0) + 1
            )
            if response.status_code == 429:
                body = response.json()
                error_codes.append(body.get("code", ""))

        expected_ok = min(count, rate)
        expected_429 = max(0, count - rate)

        # --- Status distribution matches capacity arithmetic.
        assert status_counts.get(201, 0) == expected_ok, (
            f"expected {expected_ok} 201s for rate={rate}, count={count}, "
            f"got {status_counts!r}"
        )
        assert status_counts.get(429, 0) == expected_429, (
            f"expected {expected_429} 429s for rate={rate}, count={count}, "
            f"got {status_counts!r}"
        )
        # No other statuses should appear — every request is otherwise
        # valid (distinct nickname, valid format).
        assert set(status_counts).issubset({201, 429}), (
            f"unexpected status codes in {status_counts!r}"
        )

        # --- All 429s carry the shared ``rate_limited`` error code.
        assert all(code == ErrorCode.RATE_LIMITED for code in error_codes), (
            f"expected all 429s to have code=rate_limited, got {error_codes!r}"
        )

        # --- No side effects: persisted Players equals 2xx count.
        assert _players_count(session_factory) == expected_ok


# ---------------------------------------------------------------------------
# Test 2 — POST /games per IP (Requirement 14.2, IP scope)
# ---------------------------------------------------------------------------


@given(pair=_rate_and_count())
@settings(max_examples=25, deadline=None)
def test_games_rate_limit_per_ip_rejects_over_limit_without_side_effects(
    pair: tuple[int, int],
) -> None:
    """Property 19 for ``POST /games`` under the per-IP scope.

    For per-IP capacity ``C_ip`` and ``N`` game-creation requests
    from ``N`` distinct, already-registered players sharing the same
    IP (with the per-player bucket set much larger than ``C_ip`` so
    it's the IP cap that fires), exactly ``min(N, C_ip)`` games are
    created. The rest return 429 ``rate_limited`` and do not persist
    Game rows.

    Using N distinct players avoids the Requirement 2.6 / Property 13
    "at most one in-progress game per player" rule. It also avoids
    the per-player bucket becoming the first to empty.
    """
    rate, count = pair

    # Per-IP cap pinned to ``rate``; per-player cap raised well above
    # so the IP bucket is the one that trips.
    test_settings = Settings(
        rate_limit_games_per_ip_per_minute=rate,
        # Big enough that even a single player hitting all requests
        # wouldn't run out; combined with distinct players below it's
        # unreachable.
        rate_limit_games_per_player_per_minute=60,
        # Players endpoint also needs headroom to register ``count``
        # players during setup.
        rate_limit_players_per_ip_per_minute=count + 5,
    )

    with build_test_app(settings=test_settings) as (_, client, session_factory):
        # --- Setup: register ``count`` distinct players.
        tokens: list[str] = []
        for i in range(count):
            reg = register_player(client, _unique_nickname(prefix=f"u{i}-"))
            tokens.append(reg["sessionToken"])

        # Sanity: every registration succeeded.
        assert _players_count(session_factory) == count

        # --- Send ``count`` game-creation requests, each with a
        # different player's token.
        status_counts: dict[int, int] = {}
        for token in tokens:
            response = client.post("/games", headers=auth_headers(token))
            status_counts[response.status_code] = (
                status_counts.get(response.status_code, 0) + 1
            )

        expected_ok = min(count, rate)
        expected_429 = max(0, count - rate)

        # --- Status distribution matches per-IP capacity arithmetic.
        assert status_counts.get(201, 0) == expected_ok, (
            f"expected {expected_ok} 201s for ip_cap={rate}, count={count}, "
            f"got {status_counts!r}"
        )
        assert status_counts.get(429, 0) == expected_429, (
            f"expected {expected_429} 429s for ip_cap={rate}, count={count}, "
            f"got {status_counts!r}"
        )
        assert set(status_counts).issubset({201, 429}), (
            f"unexpected status codes in {status_counts!r}"
        )

        # --- No side effects: persisted Games equals 2xx count.
        assert _games_count(session_factory) == expected_ok


# ---------------------------------------------------------------------------
# Test 3 — POST /games per player (Requirement 14.2, player scope)
# ---------------------------------------------------------------------------


@given(
    # Pin per-player capacity and total request count. The per-player
    # cap is what fires here; a low fixed cap (2..4) keeps the
    # post-429 region small and the example cheap.
    player_cap=st.integers(min_value=2, max_value=4),
    count=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=25, deadline=None)
def test_games_rate_limit_per_player_rejects_over_limit_without_side_effects(
    player_cap: int,
    count: int,
) -> None:
    """Property 19 for ``POST /games`` under the per-player scope.

    For per-player capacity ``C_pl`` and ``N`` game-creation requests
    from the SAME authenticated player (with the per-IP cap raised
    high enough that the player bucket is the one to fire), the first
    ``min(N, C_pl)`` requests are allowed by the rate limiter, and
    the remaining ``max(0, N - C_pl)`` return 429 ``rate_limited``.

    Note that the first "allowed" request creates a ``pending`` Game,
    and subsequent "allowed" requests then collide with Requirement
    2.6 / Property 13 and return 409 ``game_conflict``. The rate
    limiter runs *before* the service layer (Requirement 14.3 /
    Property 19) so the 429 count is independent of the 409 count: we
    verify the 429 count matches the over-limit arithmetic, and that
    the total number of persisted Game rows is at most 1 (only the
    first non-429 request was allowed to create a Game).
    """
    test_settings = Settings(
        rate_limit_games_per_player_per_minute=player_cap,
        # Per-IP cap high enough that it's not the binding constraint.
        rate_limit_games_per_ip_per_minute=player_cap * 10 + 10,
        rate_limit_players_per_ip_per_minute=5,
    )

    with build_test_app(settings=test_settings) as (_, client, session_factory):
        reg = register_player(client, _unique_nickname())
        headers = auth_headers(reg["sessionToken"])

        status_counts: dict[int, int] = {}
        for _ in range(count):
            response = client.post("/games", headers=headers)
            status_counts[response.status_code] = (
                status_counts.get(response.status_code, 0) + 1
            )

        expected_429 = max(0, count - player_cap)

        # --- 429 count matches the over-limit arithmetic.
        assert status_counts.get(429, 0) == expected_429, (
            f"expected {expected_429} 429s for player_cap={player_cap}, "
            f"count={count}, got {status_counts!r}"
        )

        # --- All non-429 responses are either 201 (first allowed,
        # creates the Game) or 409 (subsequent allowed-by-limiter
        # requests that hit the "one in-progress game per player"
        # rule). No other status should appear.
        assert set(status_counts).issubset({201, 409, 429}), (
            f"unexpected status codes in {status_counts!r}"
        )

        # --- At most one Game was persisted: the first allowed request
        # created a pending game; every subsequent allowed request hit
        # 409 before creating a Game. Rejected (429) requests create
        # nothing (Requirement 14.3 / Property 19).
        assert _games_count(session_factory) <= 1

        # If any request was allowed by the limiter at all, exactly one
        # Game was created (the first one). Otherwise none.
        allowed_by_limiter = min(count, player_cap)
        if allowed_by_limiter >= 1:
            assert _games_count(session_factory) == 1
            # And the 201-count is exactly 1 (the first allowed
            # request) regardless of how many subsequent allowed
            # requests existed.
            assert status_counts.get(201, 0) == 1
            # The remaining (allowed_by_limiter - 1) requests should
            # have returned 409 game_conflict.
            assert status_counts.get(409, 0) == allowed_by_limiter - 1
        else:
            assert _games_count(session_factory) == 0
            assert status_counts.get(201, 0) == 0
            assert status_counts.get(409, 0) == 0
