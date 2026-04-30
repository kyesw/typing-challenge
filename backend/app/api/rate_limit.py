"""In-process token-bucket rate limiter for the API edge (task 9.1).

This module implements a small, thread-safe token-bucket limiter used
by the FastAPI dependency layer (see
:mod:`app.api.dependencies`) to rate-limit:

* ``POST /players`` per source IP (Requirement 14.1)
* ``POST /games`` per source IP and per ``playerId`` (Requirement 14.2)

Key design choices:

- **Token-bucket (not fixed-window).** Requirement 14.3 requires that
  over-limit requests are rejected; it does not mandate a windowing
  model. A token bucket is simple, permits short bursts up to
  ``capacity``, and refills continuously at ``refill_per_sec``.
- **Per-key buckets.** Buckets are created lazily on first access for
  each ``key``. The ``key`` is opaque: callers pass a formatted string
  such as ``"ip:203.0.113.5"`` or ``"player:<uuid>"`` so multiple
  limiter instances can share the same keyspace without collision.
- **Rejected requests DO NOT consume tokens.** Requirement 14.3 and
  Property 19 both require no side effects on an over-limit request.
  The limiter models this by treating token inventory as *withdrawn*
  only when ``try_acquire`` returns ``True`` — a rejected attempt
  leaves the bucket's state unchanged (it is only refilled based on
  elapsed time, never decremented for a rejection).
- **Thread-safe.** FastAPI's default TestClient and production uvicorn
  workers may handle requests on threads; we guard the bucket map and
  each bucket's state with :class:`threading.Lock`.
- **Injectable clock.** ``clock`` defaults to :func:`time.monotonic` so
  the limiter is immune to wall-clock jumps. Tests inject a
  controllable clock so Hypothesis can drive the state machine
  deterministically.

Requirements addressed:
- 14.1 (POST /players per source IP)
- 14.2 (POST /games per source IP and per Player)
- 14.3 (rate-limit responses, no side effects on over-limit)
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def per_minute_to_per_sec(n: int) -> float:
    """Convert a "per minute" limit to a per-second refill rate.

    The Settings surface expresses all rate limits as "requests per
    minute"; the bucket works in per-second tokens. This helper keeps
    the conversion in one place so a change of unit never slips.
    """
    if n <= 0:
        raise ValueError("rate limit must be positive, got %r" % (n,))
    return float(n) / 60.0


# ---------------------------------------------------------------------------
# Internal bucket record
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """Mutable per-key bucket state.

    ``tokens`` is stored as a float because continuous refill produces
    fractional inventory between whole-token requests. ``last_refill``
    is the monotonic time of the most recent refill; the next refill
    computes elapsed relative to this timestamp and bumps it forward.
    """

    tokens: float
    last_refill: float


# ---------------------------------------------------------------------------
# Limiter
# ---------------------------------------------------------------------------


class TokenBucketLimiter:
    """Thread-safe per-key token-bucket rate limiter.

    Args:
        capacity: Maximum tokens any single bucket may hold. Also the
            starting inventory of a new bucket — a fresh caller can
            immediately burst up to ``capacity`` consecutive requests
            before being throttled.
        refill_per_sec: Continuous refill rate in tokens per second.
        clock: Zero-argument callable returning a monotonic-ish
            timestamp. Defaults to :func:`time.monotonic`. Tests inject
            a controllable clock.

    Usage::

        limiter = TokenBucketLimiter(capacity=10, refill_per_sec=10/60)
        if not limiter.try_acquire("ip:203.0.113.5"):
            raise RateLimited(...)
    """

    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive, got %r" % (capacity,))
        if refill_per_sec <= 0:
            raise ValueError(
                "refill_per_sec must be positive, got %r" % (refill_per_sec,)
            )
        self._capacity = float(capacity)
        self._refill_per_sec = float(refill_per_sec)
        self._clock = clock
        # One global lock guards both the map mutation (creation of a
        # bucket for a new key) and the per-bucket state update. A
        # single lock is sufficient for the expected edge-side load;
        # per-bucket locks would only matter at thousands of keys.
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        """Maximum tokens a bucket can hold."""
        return int(self._capacity)

    @property
    def refill_per_sec(self) -> float:
        """Continuous refill rate (tokens per second)."""
        return self._refill_per_sec

    def try_acquire(self, key: str) -> bool:
        """Attempt to consume one token for ``key``.

        Returns ``True`` if a token was available (and has been
        consumed). Returns ``False`` if the bucket is empty; in that
        case the bucket state is **not** decremented, satisfying
        Requirement 14.3 / Property 19 ("no side effects on
        over-limit").

        The refill step runs unconditionally before the availability
        check so buckets never lose tokens accrued during quiet
        periods — but tokens are capped at ``capacity`` so an idle
        caller cannot accumulate an unbounded burst.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # A new caller starts with a full bucket. That matches
                # the intuitive "first N requests are free" behavior
                # that a fixed-window limiter would also provide on
                # the first tick.
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[key] = bucket

            # Refill based on elapsed time. ``max(0, ...)`` guards
            # against a clock that went backwards (shouldn't happen
            # with ``time.monotonic`` but an injected clock might).
            elapsed = max(0.0, now - bucket.last_refill)
            if elapsed > 0.0:
                bucket.tokens = min(
                    self._capacity,
                    bucket.tokens + elapsed * self._refill_per_sec,
                )
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            # Rejected: leave ``tokens`` untouched beyond the refill.
            return False

    def reset(self) -> None:
        """Drop all bucket state. Intended for tests."""
        with self._lock:
            self._buckets.clear()


__all__ = [
    "TokenBucketLimiter",
    "per_minute_to_per_sec",
]
