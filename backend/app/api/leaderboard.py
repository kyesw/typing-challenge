"""``GET /leaderboard`` — current leaderboard snapshot (task 8.5 / 10.1).

Delegates to :meth:`LeaderboardService.build_snapshot` and repackages
the result as a :class:`LeaderboardResponse`. Deliberately does not
apply a ``limit``: the lounge scale keeps the full list small, and
Dashboard_Clients can truncate client-side.

Polling model (task 10.1):
    The Dashboard_Client polls this endpoint once per second while
    mounted (Requirement 6.2). Each call recomputes the snapshot
    directly from the ``scores`` table — no application-layer cache
    and no HTTP cache-control headers. The v1 deployment is a single
    backend with no proxy or CDN in front, and a `fetch()` GET
    without freshness hints is not cached by browsers in practice,
    so extra headers buy nothing and add noise.

Requirements addressed:
- 5.6  (GET /leaderboard returns the current snapshot)
- 6.1, 6.2 (Dashboard polling reads the latest snapshot each tick)
"""

from __future__ import annotations

from fastapi import APIRouter, status

from .dependencies import LeaderboardServiceDep
from .schemas import LeaderboardEntryResponse, LeaderboardResponse


router = APIRouter(tags=["leaderboard"])


@router.get(
    "/leaderboard",
    status_code=status.HTTP_200_OK,
    response_model=LeaderboardResponse,
    response_model_by_alias=True,
)
def get_leaderboard(
    leaderboard_service: LeaderboardServiceDep,
) -> LeaderboardResponse:
    """Return the current leaderboard snapshot.

    Each call recomputes the snapshot from the Scores table; no cache
    sits between the Dashboard_Client's 1 Hz poll and the database
    (Requirement 6.2). At lounge scale that aggregation is cheap
    enough that a cache is not warranted.
    """
    snapshot = leaderboard_service.build_snapshot()
    return LeaderboardResponse(
        entries=[
            LeaderboardEntryResponse(
                player_id=entry.player_id,
                nickname=entry.nickname,
                best_wpm=entry.best_wpm,
                best_accuracy=entry.best_accuracy,
                best_points=entry.best_points,
                rank=entry.rank,
            )
            for entry in snapshot.entries
        ],
        generated_at=snapshot.generated_at,
    )


__all__ = ["router"]
