"""Leaderboard_Service — derived leaderboard snapshot (task 6.1).

Owns the read-path that turns the persisted ``scores`` table into a
ranked list of :class:`LeaderboardEntry` rows for ``GET /leaderboard``
(task 8.5) and the dashboard's initial-snapshot fetch (task 13.1).

The entries themselves are purely derived — Requirement 5.5 forbids
them from being independently writable — so this service reads only
and performs no mutation of the Data_Store.

Requirements addressed:

- 5.1  (one LeaderboardEntry per Player with ≥1 completed Score)
- 5.2  (``bestPoints`` / ``bestWpm`` / ``bestAccuracy`` are the per-player
       maxima across that player's Scores)
- 5.3  (order by ``bestPoints desc, bestWpm desc, earliest createdAt asc``)
- 5.4  (assign contiguous ranks starting at 1)
- 5.5  (read-only derivation; no entry writes)

Design notes
------------

- **Python-side aggregation.** The tie-break "earliest ``createdAt`` of
  a Score that achieved the player's ``bestPoints``" is awkward to
  express portably in a single SQL window query against SQLite.
  Volumes are small (a lounge, tens of players, hundreds of scores at
  most per session), so we load Scores + Player.nickname in one query
  and aggregate in Python. If volumes grow, this method is the point
  to push the aggregation into SQL.
- **Ordering is exact.** ``bestWpm`` and ``bestAccuracy`` are *floats*
  persisted by the Scoring_Service; the leaderboard ordering only
  breaks ties on ``bestPoints`` (int) and ``bestWpm`` (float). Float
  equality at the 2nd tie-break would be rare but is not dangerous —
  if two players hit identical ``bestWpm`` the 3rd tie-break on
  ``first_best_at`` is deterministic and total (``datetime`` is
  totally ordered), so ranks are stable.
- **``first_best_at`` semantics.** It is the earliest ``created_at`` of
  any Score by that player whose ``points`` equals the player's
  ``best_points``. That is the tie-break Requirement 5.3 asks for
  ("earliest Score createdAt"). It is NOT the earliest of all the
  player's scores — only those that hit the best.
- **Session-per-call.** Matches the convention in
  :class:`PlayerService` / :class:`GameService.create_game`: open a
  fresh session, read, close. Keeps the service safe to share across
  FastAPI workers and trivial to test with an in-memory engine.
- **Stateless, no cache.** Every :meth:`build_snapshot` call
  recomputes the ranked list from ``scores``. For the lounge
  scenario (tens of players, hundreds of scores at most per session)
  a per-call aggregation under 1 Hz polling is cheap enough that a
  cache would add complexity without a measurable win.
- **``limit`` is post-rank.** The ``limit`` keyword truncates the
  already-ranked list to the top-N. Ranks are assigned before
  truncation so the ``N``-th entry always reports its absolute rank.
- **Clock injection.** Matches :class:`PlayerService` and
  :class:`GameService`. The clock is only used to stamp
  ``LeaderboardSnapshot.generated_at`` for diagnostics; it never
  affects the ranking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import Player, Score


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaderboardEntry:
    """A single ranked row on the leaderboard (design Model 5).

    Attributes:
        player_id: Opaque id of the Player the entry represents.
        nickname: Player's display nickname, denormalized from
            ``players.nickname`` at snapshot time.
        best_points: Maximum ``points`` across the Player's Scores
            (Requirement 5.2). Integer; primary sort key.
        best_wpm: Maximum ``wpm`` across the Player's Scores. Float;
            secondary sort key (Requirement 5.3).
        best_accuracy: Maximum ``accuracy`` across the Player's Scores.
            Float in ``[0, 100]``. Not a sort key, exposed for display
            (Requirement 6.2).
        rank: 1-based rank after ordering. Ranks are contiguous across
            the returned entries (Requirement 5.4).
        first_best_at: ``created_at`` of the earliest Score by this
            Player that matched ``best_points``. Used for the final
            tie-break (Requirement 5.3) and exposed so callers /
            tests can assert ordering.
    """

    player_id: str
    nickname: str
    best_points: int
    best_wpm: float
    best_accuracy: float
    rank: int
    first_best_at: datetime


@dataclass(frozen=True)
class LeaderboardSnapshot:
    """A ranked snapshot of the derived leaderboard.

    Attributes:
        entries: Ranked entries in display order. Empty when no
            Scores exist. Truncated to ``limit`` when one was
            supplied to :meth:`LeaderboardService.build_snapshot`.
        generated_at: Server clock when the snapshot was built.
            Exposed for diagnostics.
    """

    entries: list[LeaderboardEntry]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Timezone-aware UTC clock — matches ``DateTime(timezone=True)``."""
    return datetime.now(timezone.utc)


@dataclass
class _PerPlayerAccum:
    """Mutable accumulator used by :meth:`build_snapshot`.

    Kept module-private: it is an implementation detail of the
    aggregation loop and must not leak out of the service.
    """

    player_id: str
    nickname: str
    best_points: int
    best_wpm: float
    best_accuracy: float
    # The earliest ``created_at`` among scores where ``points ==
    # best_points``. Updated in lock-step with ``best_points``: when
    # a strictly-greater ``best_points`` is seen, the field is reset
    # to that score's ``created_at``; on a tie at ``best_points``,
    # the earlier of the two timestamps wins.
    first_best_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LeaderboardService:
    """Builds a :class:`LeaderboardSnapshot` from persisted Scores.

    Read-only: no method on this service writes to the Data_Store
    (Requirement 5.5).
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: Zero-arg callable returning a new
                SQLAlchemy :class:`Session`. Used as a context
                manager per call.
            clock: Zero-arg callable returning a timezone-aware
                ``datetime``. Used only to stamp
                :attr:`LeaderboardSnapshot.generated_at`. Defaults
                to ``datetime.now(timezone.utc)``.
        """
        self._session_factory = session_factory
        self._clock = clock

    # ------------------------------------------------------------------
    # build_snapshot
    # ------------------------------------------------------------------

    def build_snapshot(self, *, limit: int | None = None) -> LeaderboardSnapshot:
        """Compute the current leaderboard snapshot.

        Aggregates ``scores`` per ``player_id``, computes the three
        per-player maxima plus the earliest ``created_at`` of a
        best-points Score, then sorts and ranks.

        Ordering (Requirement 5.3):
            1. ``best_points`` descending
            2. ``best_wpm`` descending
            3. ``first_best_at`` ascending (earlier is better)

        Ranks are assigned in display order and are contiguous
        (``1, 2, ..., N``) — Requirement 5.4. Ties do NOT share a
        rank; the tie-break chain above is total enough for ranks
        to be distinct in any realistic input.

        Args:
            limit: If given and non-negative, truncate the returned
                entries to the top-``limit`` rows. Ranks are computed
                before truncation, so the last returned entry always
                reports its true rank within the full leaderboard.
                A value of ``None`` (default) returns all entries.
                A value of ``0`` returns an empty list.

        Returns:
            A :class:`LeaderboardSnapshot` with ranked entries in
            display order and a ``generated_at`` server timestamp.
        """
        now = self._clock()

        with self._session_factory() as session:
            # Pull every (score, nickname) pair in one query. Ordering
            # is only for determinism under test — the Python loop
            # below handles the real aggregation, so any stable order
            # works. We pick ``created_at ASC`` so that when we walk
            # rows, ties on ``points`` are first seen in chronological
            # order; that makes the "earliest created_at" tie-break
            # fall out naturally without a separate pass.
            rows = session.execute(
                select(
                    Score.player_id,
                    Score.points,
                    Score.wpm,
                    Score.accuracy,
                    Score.created_at,
                    Player.nickname,
                )
                .join(Player, Player.id == Score.player_id)
                .order_by(Score.created_at.asc())
            ).all()

            accums: dict[str, _PerPlayerAccum] = {}
            for player_id, points, wpm, accuracy, created_at, nickname in rows:
                existing = accums.get(player_id)
                if existing is None:
                    accums[player_id] = _PerPlayerAccum(
                        player_id=player_id,
                        nickname=nickname,
                        best_points=points,
                        best_wpm=wpm,
                        best_accuracy=accuracy,
                        first_best_at=created_at,
                    )
                    continue

                # Per-player maxima are independent across the three
                # fields (Requirement 5.2 / Property 9): a player's
                # best_wpm need not come from the same Score as their
                # best_points.
                if wpm > existing.best_wpm:
                    existing.best_wpm = wpm
                if accuracy > existing.best_accuracy:
                    existing.best_accuracy = accuracy

                # ``best_points`` + ``first_best_at`` move together:
                #   - strictly greater points → reset best & timestamp
                #   - tie on points           → keep the earlier ts
                #   - smaller points          → nothing
                if points > existing.best_points:
                    existing.best_points = points
                    existing.first_best_at = created_at
                elif points == existing.best_points:
                    if created_at < existing.first_best_at:
                        existing.first_best_at = created_at

            # Sort by (best_points desc, best_wpm desc, first_best_at asc).
            # ``-points`` + ``-wpm`` yields the descending primaries;
            # ``first_best_at`` sorts ascending as-is. Python's sort is
            # stable, so any residual ties are resolved by insertion
            # order — which, given the ``created_at ASC`` query above,
            # corresponds to "player whose first score came first",
            # another reasonable deterministic tie-break.
            ordered = sorted(
                accums.values(),
                key=lambda a: (-a.best_points, -a.best_wpm, a.first_best_at),
            )

            entries = [
                LeaderboardEntry(
                    player_id=a.player_id,
                    nickname=a.nickname,
                    best_points=a.best_points,
                    best_wpm=a.best_wpm,
                    best_accuracy=a.best_accuracy,
                    rank=idx + 1,
                    first_best_at=a.first_best_at,
                )
                for idx, a in enumerate(ordered)
            ]

        if limit is not None:
            # ``None`` → full list (guarded above); ``0`` or positive →
            # list slice. Negative values are treated as 0; a negative
            # limit has no useful meaning for a "top-N" API.
            if limit <= 0:
                entries = []
            else:
                entries = entries[:limit]

        return LeaderboardSnapshot(entries=entries, generated_at=now)


__all__ = [
    "LeaderboardEntry",
    "LeaderboardService",
    "LeaderboardSnapshot",
]
