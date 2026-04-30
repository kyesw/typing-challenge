"""HTTP API package: FastAPI routers and shared request dependencies.

Contains the REST endpoints that wire the service layer
(:mod:`app.services`) to the shared error contract
(:mod:`app.errors`). Routers are split by resource:

- :mod:`.players` — ``POST /players`` (task 8.1).
- :mod:`.games` — ``POST /games``, ``POST /games/{id}/begin``,
  ``POST /games/{id}/result``, ``GET /games/{id}`` (tasks 8.2-8.4,
  8.6).
- :mod:`.leaderboard` — ``GET /leaderboard`` (task 8.5).

Shared dependencies — the cross-cutting Session_Token check
``require_player`` (task 8.7) plus the per-request service
factories — live in :mod:`.dependencies` so every router imports
the same adapters.

Requirements addressed:
- 1.2, 1.3, 1.5, 1.6, 1.7, 1.8 (``POST /players``)
- 2.2, 2.3, 2.4, 2.6, 7.2 (``POST /games``)
- 3.2, 6.4, 8.2, 15.1 (``POST /games/{id}/begin``)
- 3.5, 3.6, 4.6, 4.7, 9.2 (``POST /games/{id}/result``)
- 5.6, 5.7 (``GET /leaderboard``)
- 12.1, 12.2 (``GET /games/{id}``)
- 7.2, 7.3 (``require_player`` dependency)
"""

from __future__ import annotations

from .dependencies import require_player
from .games import router as games_router
from .leaderboard import router as leaderboard_router
from .players import router as players_router

__all__ = [
    "games_router",
    "leaderboard_router",
    "players_router",
    "require_player",
]
