"""Service layer package.

Re-exports the service classes and their result types so callers can
import them as ``from app.services import PlayerService``.

Requirements addressed:
- 1.3, 7.1, 7.5, 16.1, 16.2 (PlayerService.register — task 3.1)
- 7.2, 7.3, 7.5 (PlayerService.authorize — task 3.2)
- 2.3, 2.4, 2.6, 8.1, 8.6, 11.1 (GameService.create_game — task 4.2)
- 3.2, 8.2, 15.1 (GameService.begin_typing — task 4.3)
- 3.6, 4.4, 4.5, 8.3, 8.7, 9.2, 15.1, 15.2 (GameService.complete — task 4.4)
- 9.1, 9.4 (GameService.sweep_timeouts + TimeoutSweeper — task 4.5)
- 3.6, 4.4, 4.5, 15.1, 15.2 (ScoringService.compute_and_persist — task 5.2)
- 5.1, 5.2, 5.3, 5.4, 5.5 (LeaderboardService.build_snapshot — task 6.1)
"""

from __future__ import annotations

from .leaderboard_service import (
    LeaderboardEntry,
    LeaderboardService,
    LeaderboardSnapshot,
)
from .game_service import (
    BeginTypingResult,
    BeginTypingSuccess,
    CompleteGameResult,
    CompleteGameSuccess,
    CompleteGameTimeout,
    CreateGameResult,
    CreateGameSuccess,
    GameAlreadyInProgress,
    GameNotFound,
    GameNotInPending,
    GameNotInProgress,
    GameService,
    PlayerNotFound,
    SweptGame,
)
from .player_service import (
    AuthorizationResult,
    AuthorizedPlayer,
    NicknameTaken,
    NicknameValidationError,
    PlayerService,
    RegistrationResult,
    RegistrationSuccess,
    Unauthorized,
)
from .scoring_service import (
    GameNotEligible,
    RecordScoreResult,
    RecordScoreSuccess,
    ScoreAlreadyExists,
    ScoringService,
)
from .timeout_sweeper import TimeoutSweeper

__all__ = [
    "AuthorizationResult",
    "AuthorizedPlayer",
    "BeginTypingResult",
    "BeginTypingSuccess",
    "CompleteGameResult",
    "CompleteGameSuccess",
    "CompleteGameTimeout",
    "CreateGameResult",
    "CreateGameSuccess",
    "GameAlreadyInProgress",
    "GameNotEligible",
    "GameNotFound",
    "GameNotInPending",
    "GameNotInProgress",
    "GameService",
    "LeaderboardEntry",
    "LeaderboardService",
    "LeaderboardSnapshot",
    "NicknameTaken",
    "NicknameValidationError",
    "PlayerNotFound",
    "PlayerService",
    "RecordScoreResult",
    "RecordScoreSuccess",
    "RegistrationResult",
    "RegistrationSuccess",
    "ScoreAlreadyExists",
    "ScoringService",
    "SweptGame",
    "TimeoutSweeper",
    "Unauthorized",
]
