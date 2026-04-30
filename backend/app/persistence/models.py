"""SQLAlchemy ORM models for Player, Prompt, Game, and Score.

Design decisions (v1, SQLite, single-instance lounge deployment):

- **Primary keys** are opaque UUIDv4 strings (``str``). This matches the
  API contract, which exposes ``playerId`` / ``gameId`` as strings, and
  avoids leaking a monotonically-increasing integer that a client could
  probe. All four tables follow the same convention.
- **Enums vs CheckConstraint.** ``games.status`` uses a SQLAlchemy
  :class:`Enum` backed by the native ``GameStatus`` Python enum. On
  SQLite this renders as a ``VARCHAR`` column with a ``CHECK (status IN
  (...))`` constraint, which is the idiomatic way to enforce Requirement
  8's four-value invariant. ``prompts.difficulty`` is modelled the same
  way but nullable — Requirement 11.4 only constrains the value *when
  present*.
- **``ended_at > started_at`` invariant** (Requirement 8.7 / Property 8)
  is enforced by a named ``CheckConstraint`` on ``games``. Because both
  columns are nullable (status transitions set them at different
  points), the predicate is guarded with ``OR started_at IS NULL OR
  ended_at IS NULL`` so ``pending`` and ``in_progress`` rows can be
  inserted without tripping it.
- **Score numeric ranges** (Requirement 4.1 / 4.2) are enforced by
  ``CheckConstraint`` on ``wpm >= 0`` and ``accuracy`` in ``[0, 100]``.
- **Case-insensitive nickname uniqueness** (Requirement 1.7) is
  implemented by a second column, ``nickname_ci``, that the service
  layer will populate with ``nickname.casefold()`` (or ``.lower()``) and
  that carries the ``UNIQUE`` constraint. Keeping the original casing in
  ``nickname`` preserves the display name (Requirement 1.3 asks for the
  submitted nickname to be stored).
- **Leaderboard indexes** (Requirement 5.7 / design "Performance
  Considerations"): explicit indexes on ``scores.player_id``,
  ``scores.points``, and ``scores.created_at`` support the ORDER BY
  ``best_points DESC, best_wpm DESC, created_at ASC`` query without a
  table scan. An additional index on ``players.session_token`` makes
  per-request authorization lookups cheap.
- **Player column allowlist** (Requirement 16.2 / Property 20): the
  ``players`` table contains **only** ``id``, ``nickname``,
  ``nickname_ci``, ``created_at``, ``session_token``, and
  ``session_expires_at``. No additional personal data columns.

Requirements addressed:
- 1.3, 1.7 (Player identity + case-insensitive nickname uniqueness)
- 2.3     (Game created with status ``pending``, references player & prompt)
- 4.4     (Exactly one Score per Game — ``UNIQUE`` on ``game_id``)
- 4.5, 8.7 (``ended_at > started_at`` invariant)
- 11.2    (Prompt identity + language)
- 16.2    (Player record column allowlist)
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class GameStatus(str, enum.Enum):
    """Status values allowed on a Game (Requirement 8.1–8.5)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PromptDifficulty(str, enum.Enum):
    """Difficulty values allowed on a Prompt (Requirement 11.4).

    Stored as a SQL Enum when present; ``prompts.difficulty`` is nullable
    because Requirement 11.4 only constrains the value *when present*.
    """

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------


class Player(Base):
    """Registered player for the current lounge session.

    The column set is intentionally minimal — see Requirement 16.2 /
    Property 20. Do not add columns here without updating the
    corresponding property test in ``tests/test_persistence.py``.
    """

    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(20), nullable=False)
    # Case-folded copy of ``nickname`` used to enforce case-insensitive
    # uniqueness per Requirement 1.7. The service layer is responsible
    # for keeping ``nickname_ci`` in sync with ``nickname``.
    nickname_ci: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    session_token: Mapped[str] = mapped_column(String(64), nullable=False)
    session_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Relationships (lazy, for convenience in the service layer).
    games: Mapped[list["Game"]] = relationship(back_populates="player")
    scores: Mapped[list["Score"]] = relationship(back_populates="player")

    __table_args__ = (
        # Session tokens are looked up on every authorized request;
        # keep this an explicit index so it survives table re-creation.
        Index("ix_players_session_token", "session_token"),
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class Prompt(Base):
    """Typing passage served to a Game.

    Length-range enforcement for ``text`` (``[100, 500]``) is performed
    in the validator (task 2.4), not at the DB layer — the service layer
    rejects invalid prompts before they are ever inserted. The DB only
    enforces the difficulty enum per Requirement 11.4.
    """

    __tablename__ = "prompts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    difficulty: Mapped[PromptDifficulty | None] = mapped_column(
        Enum(PromptDifficulty, name="prompt_difficulty", native_enum=False),
        nullable=True,
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False)


# ---------------------------------------------------------------------------
# Game
# ---------------------------------------------------------------------------


class Game(Base):
    """A single attempt by a player to type a prompt.

    Invariants enforced here:

    - ``status`` is one of the ``GameStatus`` values (Requirement 8).
    - When both are set, ``ended_at > started_at`` (Requirement 8.7 /
      Property 8). The check tolerates either column being NULL so that
      ``pending`` and ``in_progress`` rows insert cleanly.
    """

    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    player_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
    )
    prompt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("prompts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[GameStatus] = mapped_column(
        Enum(GameStatus, name="game_status", native_enum=False),
        nullable=False,
        default=GameStatus.PENDING,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    player: Mapped[Player] = relationship(back_populates="games")
    score: Mapped["Score | None"] = relationship(
        back_populates="game",
        uselist=False,
    )

    __table_args__ = (
        CheckConstraint(
            "started_at IS NULL OR ended_at IS NULL OR ended_at > started_at",
            name="ck_games_ended_after_started",
        ),
        Index("ix_games_player_id", "player_id"),
    )


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


class Score(Base):
    """Computed outcome of a completed Game.

    ``game_id`` is ``UNIQUE`` to enforce Requirement 4.4 / Property 8
    ("exactly one Score per completed Game"). ``player_id`` is
    denormalized onto this table so the leaderboard query can aggregate
    per player without joining through ``games`` (design Model 4 +
    Performance Considerations).
    """

    __tablename__ = "scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    game_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("games.id", ondelete="CASCADE"),
        nullable=False,
    )
    player_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
    )
    wpm: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )

    game: Mapped[Game] = relationship(back_populates="score")
    player: Mapped[Player] = relationship(back_populates="scores")

    __table_args__ = (
        UniqueConstraint("game_id", name="uq_scores_game_id"),
        CheckConstraint("wpm >= 0", name="ck_scores_wpm_nonneg"),
        CheckConstraint(
            "accuracy >= 0 AND accuracy <= 100",
            name="ck_scores_accuracy_range",
        ),
        # Leaderboard query indexes (Requirement 5.7).
        Index("ix_scores_player_id", "player_id"),
        Index("ix_scores_points", "points"),
        Index("ix_scores_created_at", "created_at"),
    )


__all__ = [
    "GameStatus",
    "PromptDifficulty",
    "Player",
    "Prompt",
    "Game",
    "Score",
]
