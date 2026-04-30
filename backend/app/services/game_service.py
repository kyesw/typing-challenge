"""Game_Service — lifecycle of a single typing Game (tasks 4.2, 4.3, 4.4, 4.5).

This module implements :meth:`GameService.create_game` (task 4.2),
:meth:`GameService.begin_typing` (task 4.3),
:meth:`GameService.complete` (task 4.4), and the synchronous
:meth:`GameService.sweep_timeouts` entry point used by the async
sweeper loop in :mod:`app.services.timeout_sweeper` (task 4.5).

Responsibilities covered by this task:

- Requirements 2.3 / 2.4 / 8.1: create a Game row with status
  ``pending``, an assigned ``promptId`` from the Prompt_Repository, a
  reference to the player, and return the ``gameId``, prompt text, and
  a server-determined reference clock.
- Requirements 2.6 / 8.6: reject creation when the player already has a
  Game in status ``pending`` or ``in_progress`` and return the existing
  ``gameId`` in the conflict payload.
- Requirement 11.1: delegate prompt selection to the Prompt_Repository
  rather than picking one in-line, so the selection policy can change
  without touching this service.

Design notes:

- **Conflict on pending OR in_progress.** Requirement 2.6 names
  ``in_progress`` explicitly, and Requirement 8.6 caps at-most-one
  ``in_progress`` per player. But a ``pending`` Game is the row the
  :meth:`create_game` path itself writes, and the state machine in
  :mod:`app.domain.game_state` treats ``pending`` as the on-ramp to
  ``in_progress``. Creating a second ``pending`` row while another is
  live would leave orphan rows that the timeout sweeper has no basis
  to clean up (neither ``started_at`` nor a begin event exists yet).
  So this service treats either state as "already has a live game" and
  returns the existing ``gameId`` — the client can then decide to
  resume, abandon, or wait. This is consistent with design Error
  Scenario 3 ("API returns a conflict response including the existing
  gameId").
- **Completed and abandoned do NOT block.** Only the two non-terminal
  statuses participate in the conflict check. A player who just
  finished a Game (``completed``) or walked away from one
  (``abandoned``) must be able to start a fresh one immediately.
- **Clock injection.** Matches :class:`app.services.player_service.PlayerService`.
  The clock is called once per :meth:`create_game` to produce the
  reserved reference timestamp returned to the client; the DB row's
  ``started_at`` remains ``NULL`` (Game is ``pending``) until
  ``begin_typing`` sets it for real (task 4.3).
- **Player existence.** Creation is gated on the player actually
  existing in the ``players`` table. The API layer's ``require_player``
  dependency (task 8.7) will normally ensure this, but the service
  layer is the authoritative guard so unit tests and any non-HTTP
  caller see the same behaviour. A missing player returns
  :class:`PlayerNotFound`, not a generic conflict.
- **No ORM leak.** The returned success carries plain fields (ids,
  strings, a timezone-aware datetime). We never hand the caller an ORM
  instance whose session has already closed.

Requirements addressed:
- 2.3  (Game record created with status ``pending``, promptId, playerId)
- 2.4  (Return gameId, prompt text, server-determined ``startAt``)
- 2.6  (Conflict on existing live game includes existing ``gameId``)
- 8.1  (Initial status ``pending``)
- 8.6  (At most one in-progress Game per player)
- 11.1 (Prompt supplied via Prompt_Repository policy)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..domain.game_state import (
    INITIAL_STATUS,
    GameEvent,
    GameStatus as DomainGameStatus,
    TransitionOk,
    transition,
)
from ..persistence.models import Game, GameStatus, Player, Prompt
from ..persistence.prompt_repository import PromptRepository
from .scoring_service import (
    GameNotEligible,
    RecordScoreSuccess,
    ScoreAlreadyExists,
    ScoringService,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateGameSuccess:
    """A Game was created successfully.

    Attributes:
        game_id: Opaque UUID string identifier of the new Game.
        prompt_id: Opaque UUID string id of the assigned Prompt.
        prompt_text: The passage the player will type.
        language: BCP-47-ish language code carried by the Prompt.
        status: Always :data:`GameStatus.PENDING` on success; returned
            explicitly so the API layer can echo it without a second
            lookup.
        started_at: Server-determined reference clock for the typing
            phase start. The persisted ``games.started_at`` column
            remains NULL until :meth:`GameService.begin_typing` (task
            4.3) sets it for real; this value is what the client uses
            to align its countdown with the server's view of "now"
            (Requirement 2.4).
    """

    game_id: str
    prompt_id: str
    prompt_text: str
    language: str
    status: GameStatus
    started_at: datetime


@dataclass(frozen=True)
class GameAlreadyInProgress:
    """The player already has a live (pending or in_progress) Game.

    Requirement 2.6 requires the conflict payload to include the
    existing ``gameId`` so the Web_Client can route the player into it
    or offer to abandon and restart (Requirement 2.7).

    Attributes:
        game_id: The existing live Game's id.
        status: The existing live Game's status. Either ``pending`` or
            ``in_progress``; the client may treat them differently
            (e.g., resume countdown vs. resume typing).
    """

    game_id: str
    status: GameStatus


@dataclass(frozen=True)
class PlayerNotFound:
    """The supplied ``player_id`` does not resolve to a Player row.

    The API layer maps this to 401 (via the auth dependency) in
    practice; keeping it as a distinct service-layer result lets unit
    tests and any non-HTTP caller handle it explicitly.

    Attributes:
        player_id: The id that was looked up, echoed for logging /
            diagnostics.
    """

    player_id: str


CreateGameResult = CreateGameSuccess | GameAlreadyInProgress | PlayerNotFound


# ---------------------------------------------------------------------------
# begin_typing result types (task 4.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BeginTypingSuccess:
    """The Game transitioned ``pending → in_progress``.

    Attributes:
        game_id: The Game's id, echoed for convenience so the caller
            does not have to carry the input through separately.
        status: Always :data:`GameStatus.IN_PROGRESS` on success.
        started_at: The server clock recorded at transition time and
            persisted to ``games.started_at``. This is the
            authoritative typing-phase start used by the Scoring_Service
            when it computes elapsed time (Requirement 15.1 / 15.2).
        prompt_id: The assigned Prompt's id. Returned so the API layer
            can echo it on the ``/begin`` response without a second
            lookup.
        prompt_text: The passage the player is typing. Returned for
            the same reason — the ``/begin`` endpoint's response
            surface mirrors the ``/games`` response (Requirement 2.4)
            so a reconnecting client can render the prompt from a
            single payload.
    """

    game_id: str
    status: GameStatus
    started_at: datetime
    prompt_id: str
    prompt_text: str


@dataclass(frozen=True)
class GameNotFound:
    """Either the Game does not exist, or it belongs to a different player.

    The two situations are collapsed into a single result intentionally:
    leaking "this Game exists but isn't yours" would let a caller enumerate
    other players' game ids via this endpoint. The API layer maps this
    to 404. See Requirement 12.2 ("not-found response") and the
    session-token scoping in Requirement 7.2.

    Attributes:
        game_id: The id that was looked up, echoed for logging /
            diagnostics.
    """

    game_id: str


@dataclass(frozen=True)
class GameNotInPending:
    """The Game exists and belongs to the player, but isn't ``pending``.

    Carries the observed current status so the API layer can map
    specific cases to a useful response:

    - ``in_progress``: idempotent-ish (already begun); the API layer
      may return 409 with the existing ``started_at``, or treat the
      call as a no-op.
    - ``completed`` / ``abandoned``: terminal; 409.

    Requirement 8.5 mandates that any transition not in the allowed
    table leaves the status unchanged, which is exactly what the
    service does when it sees this case.

    Attributes:
        game_id: The Game's id.
        current_status: The Game's status as observed under the
            transaction that detected the mismatch.
    """

    game_id: str
    current_status: GameStatus


BeginTypingResult = BeginTypingSuccess | GameNotFound | GameNotInPending


# ---------------------------------------------------------------------------
# complete result types (task 4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompleteGameSuccess:
    """The Game transitioned ``in_progress → completed`` and a Score persisted.

    The caller (API layer, task 8.4) echoes ``wpm``, ``accuracy``, and
    ``points`` back to the Web_Client; ``rank`` is looked up by the
    API against the leaderboard (task 6.1) so it stays out of this
    payload.

    Attributes:
        game_id: The Game's id, echoed for convenience.
        player_id: The owning player's id, echoed so the API can
            correlate without a second read.
        score_id: The freshly-persisted Score row's id.
        wpm: Words-per-minute from :func:`compute_wpm`.
        accuracy: Accuracy percentage in ``[0, 100]``.
        points: Deterministic points value (Requirement 4.3 /
            Property 6).
        ended_at: The server clock written to ``games.ended_at``.
        elapsed_seconds: The server-authoritative elapsed time used
            for scoring (``ended_at - started_at``). Returned for
            diagnostics; not exposed on the API response.
    """

    game_id: str
    player_id: str
    score_id: str
    wpm: float
    accuracy: float
    points: int
    ended_at: datetime
    elapsed_seconds: float


@dataclass(frozen=True)
class CompleteGameTimeout:
    """The Game exceeded Maximum_Game_Duration; it was marked abandoned.

    Requirement 9.2 / Property 14 dictate that a late submission is
    rejected and the Game transitions to ``abandoned``. The API layer
    (task 8.4) maps this to 409 with code ``game_timeout``
    (:data:`app.errors.ErrorCode.GAME_TIMEOUT`).

    Attributes:
        game_id: The abandoned Game's id.
        ended_at: The server clock at which the service detected the
            timeout and marked the Game abandoned. Written to
            ``games.ended_at``.
        elapsed_seconds: The observed elapsed time. Included so
            operators can tell how far over the limit the submission
            was.
    """

    game_id: str
    ended_at: datetime
    elapsed_seconds: float


@dataclass(frozen=True)
class GameNotInProgress:
    """The Game exists and belongs to the player, but isn't ``in_progress``.

    Collapses three sub-cases the caller typically maps to 409:

    * ``pending`` — ``begin_typing`` (task 4.3) was never called.
    * ``completed`` — the submission is a duplicate; the caller may
      treat it as idempotent or surface the conflict, its choice.
    * ``abandoned`` — already timed out or abandoned out-of-band.

    Attributes:
        game_id: The Game's id.
        current_status: The Game's status as observed inside the
            transaction that detected the mismatch.
    """

    game_id: str
    current_status: GameStatus


CompleteGameResult = (
    CompleteGameSuccess
    | CompleteGameTimeout
    | GameNotFound
    | GameNotInProgress
)


# ---------------------------------------------------------------------------
# sweep_timeouts result types (task 4.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweptGame:
    """A Game that the timeout sweeper transitioned ``in_progress → abandoned``.

    Returned by :meth:`GameService.sweep_timeouts` (one entry per Game
    that exceeded :attr:`Settings.max_game_duration_seconds`). The
    async loop in :mod:`app.services.timeout_sweeper` uses these only
    for diagnostics/logging; the service itself has already committed
    the status change before the value is returned to the caller.

    Attributes:
        game_id: The abandoned Game's id.
        player_id: The owning player's id. Included so a caller that
            wants to emit its own "player X timed out" UI hint does
            not need a second lookup.
        started_at: The Game's authoritative typing-phase start
            timestamp, echoed for diagnostics. Always non-None — a
            Game is only considered for sweeping when ``started_at``
            is set and precedes the cutoff.
        ended_at: The server clock the sweeper wrote into
            ``games.ended_at`` at the moment the Game was marked
            abandoned.
    """

    game_id: str
    player_id: str
    started_at: datetime
    ended_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Timezone-aware UTC clock — matches ``DateTime(timezone=True)``."""
    return datetime.now(timezone.utc)


def _default_id_factory() -> str:
    """Opaque UUIDv4 string id — matches the convention used elsewhere."""
    return str(uuid.uuid4())


#: Statuses treated as "live" for the purpose of the per-player
#: single-game invariant. A Game in one of these statuses blocks
#: :meth:`GameService.create_game` for its owning player. Completed and
#: abandoned games do NOT appear here — those are terminal and must not
#: block a fresh start.
_LIVE_STATUSES: tuple[GameStatus, ...] = (
    GameStatus.PENDING,
    GameStatus.IN_PROGRESS,
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GameService:
    """Owns the write-path for Game creation (task 4.2).

    The service is stateless: it opens a fresh DB session per call via
    the injected ``session_factory`` and delegates prompt selection to
    the injected :class:`PromptRepository`. That keeps the service safe
    to share across FastAPI workers and trivial to exercise from unit
    tests that wire in an in-memory engine + a fake prompt repository.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        prompt_repository: PromptRepository,
        *,
        clock: Callable[[], datetime] = _default_clock,
        id_factory: Callable[[], str] = _default_id_factory,
        scoring_service: ScoringService | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: Zero-arg callable returning a new
                SQLAlchemy :class:`Session` (typically a
                ``sessionmaker``). Used as a context manager per call.
            prompt_repository: The adapter that implements the prompt
                selection policy (Requirement 11.1). Only the
                :meth:`PromptRepository.select_prompt` method is used
                from this service.
            clock: Zero-arg callable returning a timezone-aware
                ``datetime``. Injected for tests; defaults to
                ``datetime.now(timezone.utc)``.
            id_factory: Zero-arg callable producing a fresh Game id
                string. Injected for tests; defaults to
                ``str(uuid.uuid4())``.
            scoring_service: Adapter responsible for Score
                computation + persistence (task 5.2). Required for
                :meth:`complete`; the ``create_game`` and
                ``begin_typing`` paths don't touch it. Defaults to a
                freshly constructed :class:`ScoringService` so
                existing callers that don't pass one still get a
                working ``complete`` method.
            settings: Runtime configuration. Only
                :attr:`Settings.max_game_duration_seconds` is read
                (Requirement 9.1). Defaults to the cached
                :func:`get_settings` so production callers don't have
                to pass it.
        """
        # Sanity: keep the service aligned with the domain state machine.
        # ``INITIAL_STATUS`` is the authoritative initial status per
        # Requirement 8.1; we assert structural equivalence with the
        # persistence-layer enum so a future refactor that drifts the
        # two enums is caught immediately rather than silently
        # producing the wrong initial row.
        assert INITIAL_STATUS.value == GameStatus.PENDING.value, (
            "domain INITIAL_STATUS must map to persistence GameStatus.PENDING"
        )
        # Reference the import so linters don't flag it and so a
        # future reorganization cannot drop the dependency silently.
        _ = DomainGameStatus  # noqa: F841

        self._session_factory = session_factory
        self._prompt_repository = prompt_repository
        self._clock = clock
        self._id_factory = id_factory
        self._scoring_service = (
            scoring_service if scoring_service is not None else ScoringService()
        )
        self._settings = settings if settings is not None else get_settings()

    # ------------------------------------------------------------------
    # create_game
    # ------------------------------------------------------------------

    def create_game(self, player_id: str) -> CreateGameResult:
        """Create a new Game for ``player_id``.

        Steps:

        1. Verify the player exists. Missing → :class:`PlayerNotFound`.
        2. Check for an existing live (``pending`` or ``in_progress``)
           Game for this player. Found → :class:`GameAlreadyInProgress`
           carrying the existing ``game_id`` and ``status``.
        3. Ask the Prompt_Repository for a Prompt (delegates policy
           per Requirement 11.1).
        4. Insert a Game row with a fresh id, ``status = pending``,
           both timestamps NULL (task 4.3 sets ``started_at``).
        5. Return :class:`CreateGameSuccess` with the reserved
           server-clock ``started_at`` and the prompt payload.

        Args:
            player_id: The id of the authenticated player. The API
                layer (task 8.7) supplies this from the Session_Token.

        Returns:
            A :data:`CreateGameResult` variant.
        """
        now = self._clock()
        new_game_id = self._id_factory()

        with self._session_factory() as session:
            # Step 1: player must exist. ``Session.get`` is the
            # cheapest lookup by primary key and returns ``None`` for
            # a miss without raising.
            player = session.get(Player, player_id)
            if player is None:
                return PlayerNotFound(player_id=player_id)

            # Step 2: existing live game? Any row in PENDING or
            # IN_PROGRESS for this player blocks creation. We order
            # arbitrarily; the invariant in Requirement 8.6 caps this
            # at one row per player under normal operation, so the
            # choice only matters in the (illegal) multi-live edge
            # case. LIMIT 1 keeps the query cheap either way.
            existing_stmt = (
                select(Game.id, Game.status)
                .where(
                    Game.player_id == player_id,
                    Game.status.in_(_LIVE_STATUSES),
                )
                .limit(1)
            )
            existing = session.execute(existing_stmt).first()
            if existing is not None:
                existing_id, existing_status = existing
                return GameAlreadyInProgress(
                    game_id=existing_id,
                    status=existing_status,
                )

            # Step 3: prompt selection. The repository returns a
            # detached :class:`SelectedPrompt`, so we never need to
            # reattach a prompt ORM instance to this session.
            selected = self._prompt_repository.select_prompt()

            # Step 4: insert the Game row.
            row = Game(
                id=new_game_id,
                player_id=player_id,
                prompt_id=selected.id,
                status=GameStatus.PENDING,
                started_at=None,
                ended_at=None,
            )
            session.add(row)
            session.commit()

            # Step 5: success payload. The reserved ``started_at``
            # timestamp is the server's view of "now" at creation
            # time; :meth:`GameService.begin_typing` (task 4.3) will
            # replace it with the authoritative typing-phase start.
            return CreateGameSuccess(
                game_id=new_game_id,
                prompt_id=selected.id,
                prompt_text=selected.text,
                language=selected.language,
                status=GameStatus.PENDING,
                started_at=now,
            )

    # ------------------------------------------------------------------
    # begin_typing (task 4.3)
    # ------------------------------------------------------------------

    def begin_typing(
        self,
        game_id: str,
        *,
        player_id: str,
    ) -> BeginTypingResult:
        """Mark the typing phase started for ``game_id``.

        Performs the ``pending → in_progress`` transition and records
        the authoritative ``started_at`` timestamp on the Game row.
        The Scoring_Service (task 4.4) later uses this timestamp — not
        any client-supplied clock — to compute elapsed time
        (Requirements 15.1 / 15.2).

        Steps:

        1. Load the Game by id. Missing → :class:`GameNotFound`.
        2. Verify ownership. If ``player_id`` does not match the Game's
           ``player_id``, return :class:`GameNotFound` rather than a
           distinct "not yours" error — this keeps the endpoint from
           leaking existence of other players' games.
        3. Consult the pure state machine in
           :mod:`app.domain.game_state` to validate the transition.
           Any non-pending current status returns
           :class:`GameNotInPending` carrying the observed status.
        4. On :class:`TransitionOk`, set ``status = in_progress`` and
           ``started_at = server_now()``, then commit.
        5. Return :class:`BeginTypingSuccess` with the fields the API
           layer needs to respond.

        Args:
            game_id: The id of the Game to transition.
            player_id: The authenticated player's id. Required
                keyword-only so callers cannot forget authorization —
                the API layer (task 8.3) supplies this from the
                Session_Token via the ``require_player`` dependency
                (task 8.7). The service layer is the authoritative
                ownership check so unit tests and any non-HTTP caller
                see the same behaviour.

        Returns:
            A :data:`BeginTypingResult` variant.
        """
        now = self._clock()

        with self._session_factory() as session:
            # Step 1: game must exist. ``Session.get`` returns ``None``
            # on a miss without raising, which is the cheapest lookup
            # we can do against the primary key.
            row = session.get(Game, game_id)
            if row is None:
                return GameNotFound(game_id=game_id)

            # Step 2: ownership. The endpoint must not reveal whether
            # the id exists under another player (Requirement 7.2 —
            # session-token scoping; design Error Scenario 2 general
            # "don't leak existence" principle). Folding the mismatch
            # into GameNotFound satisfies that.
            if row.player_id != player_id:
                return GameNotFound(game_id=game_id)

            # Step 3: validate against the pure state machine. Using
            # the domain helper here — instead of an ad-hoc
            # ``if status is PENDING`` check — keeps this service
            # consistent with the Property 12 test in task 4.6 and
            # with the upcoming ``complete`` / ``abandon`` methods
            # that will go through the same table.
            #
            # The domain enum (:class:`DomainGameStatus`) and the
            # persistence enum (:class:`GameStatus`) share string
            # values; we translate by value rather than relying on
            # identity so a future re-home of one of the enums
            # doesn't silently break the call.
            current_domain = DomainGameStatus(row.status.value)
            outcome = transition(current_domain, GameEvent.BEGIN_TYPING)
            if not isinstance(outcome, TransitionOk):
                # Any non-pending state lands here: in_progress (already
                # begun), completed, or abandoned. Requirement 8.5
                # leaves the row's status untouched — which is what
                # this early return accomplishes (we never write).
                return GameNotInPending(
                    game_id=game_id,
                    current_status=row.status,
                )

            # Step 4: apply the transition. The new status comes from
            # the domain helper rather than being hard-coded, so a
            # future change to the table (e.g., a different target for
            # BEGIN_TYPING) propagates here automatically.
            new_status = GameStatus(outcome.new_status.value)
            row.status = new_status
            row.started_at = now
            # ended_at stays NULL; task 4.4 sets it on completion.

            # Re-fetch the prompt payload before commit so we can
            # return it to the caller. The FK on ``games.prompt_id``
            # guarantees the row exists; we pull text + id in one
            # hop via ``Session.get``. Doing this inside the same
            # session avoids the "detached instance" class of bugs
            # the ``create_game`` docstring calls out.
            prompt_row = session.get(Prompt, row.prompt_id)
            # prompt_row cannot be None given the FK constraint, but
            # guard defensively so a corrupted DB yields a clear
            # error rather than an AttributeError.
            if prompt_row is None:  # pragma: no cover - FK invariant
                raise RuntimeError(
                    f"Game {game_id!r} references missing prompt "
                    f"{row.prompt_id!r}"
                )
            prompt_id = prompt_row.id
            prompt_text = prompt_row.text

            session.commit()

            return BeginTypingSuccess(
                game_id=game_id,
                status=new_status,
                started_at=now,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
            )

    # ------------------------------------------------------------------
    # complete (task 4.4)
    # ------------------------------------------------------------------

    def complete(
        self,
        game_id: str,
        typed_text: str,
        *,
        player_id: str,
    ) -> CompleteGameResult:
        """Finalize a typing attempt: score it, or mark it abandoned on timeout.

        This method owns the ``in_progress → completed`` path
        (happy) and the ``in_progress → abandoned`` path (timeout).
        It delegates the numeric scoring + Score persistence to
        :class:`ScoringService` (task 5.2) so the Score insert and
        the Game status update share a single transaction.

        Steps:

        1. Load the Game by id. Missing → :class:`GameNotFound`.
        2. Ownership: if ``player_id`` doesn't match the Game's
           ``player_id``, return :class:`GameNotFound` (we don't
           leak existence across players; see ``begin_typing``).
        3. Verify ``status == in_progress``. Anything else →
           :class:`GameNotInProgress` with the observed status.
        4. Verify ``started_at`` is set. If not, the row is
           structurally unscorable; we surface
           :class:`GameNotInProgress` so the caller handles it like
           any other state mismatch. (``begin_typing`` always sets
           ``started_at`` when it moves to ``in_progress``, so this
           guards against corrupted rows only.)
        5. Stamp ``ended_at = server_now()``. If ``ended_at`` isn't
           strictly greater than ``started_at`` (a clock skew or a
           zero-duration edge case the state-machine guards can't
           catch), return :class:`GameNotInProgress`. Requirement
           8.7 treats ``ended_at > started_at`` as a hard invariant,
           and the Score row's ``ck_scores_wpm_nonneg`` / the
           Game's ``ck_games_ended_after_started`` check constraints
           will reject a write that violates it.
        6. Timeout check (Requirement 9.1 / Property 14):
           ``elapsed = (ended_at - started_at).total_seconds()``.
           If ``elapsed > settings.max_game_duration_seconds``, apply
           the ``in_progress → abandoned`` transition, stamp
           ``ended_at``, commit, and return
           :class:`CompleteGameTimeout`.
        7. Otherwise, delegate to
           :meth:`ScoringService.compute_and_persist`. That method:
             * inserts a Score row in the same session;
             * applies the ``in_progress → completed`` transition
               and stamps ``ended_at`` on the Game row;
             * flushes (but does not commit) so UNIQUE violations
               land inside this transaction.
           If the Scoring_Service reports
           :class:`ScoreAlreadyExists`, we surface it as
           :class:`GameNotInProgress(current_status=IN_PROGRESS)` —
           it means another writer raced in. (In practice the step-3
           guard makes this unreachable with a single writer, but we
           handle it defensively to avoid surfacing an
           ``IntegrityError``.)
        8. Commit.
        9. Return :class:`CompleteGameSuccess` mirroring the
           Scoring_Service's payload.

        Args:
            game_id: The id of the Game to finalize.
            typed_text: The player's submitted text. Passed through
                verbatim to the Scoring_Service; API-level length
                guards (task 9.4) run before this call.
            player_id: The authenticated player's id. Required
                keyword-only so callers cannot forget authorization.

        Returns:
            A :data:`CompleteGameResult` variant.
        """
        now = self._clock()

        with self._session_factory() as session:
            # Step 1: game must exist.
            row = session.get(Game, game_id)
            if row is None:
                return GameNotFound(game_id=game_id)

            # Step 2: ownership.
            if row.player_id != player_id:
                return GameNotFound(game_id=game_id)

            # Step 3: must be in_progress.
            if row.status is not GameStatus.IN_PROGRESS:
                return GameNotInProgress(
                    game_id=game_id,
                    current_status=row.status,
                )

            # Step 4: started_at must be set. Under normal operation
            # ``begin_typing`` always sets it; a NULL here indicates
            # a corrupted row rather than a user-visible state.
            if row.started_at is None:
                # Treat a structurally unscorable row the same as a
                # status mismatch; the API layer will 409.
                return GameNotInProgress(
                    game_id=game_id,
                    current_status=row.status,
                )

            started_at = row.started_at
            # Normalize for subtraction — SQLite's ``DateTime(timezone=True)``
            # round-trips naive, so compare in UTC.
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)

            # Step 5: ended_at > started_at. We use the same ``now``
            # for the timeout calculation AND for the Score write
            # so a sub-millisecond drift cannot flip the branch.
            if now <= started_at:
                # Clock skew or an out-of-order write — refuse rather
                # than writing a row that violates Requirement 8.7.
                return GameNotInProgress(
                    game_id=game_id,
                    current_status=row.status,
                )
            elapsed_seconds = (now - started_at).total_seconds()

            # Step 6: timeout branch.
            max_duration = float(self._settings.max_game_duration_seconds)
            if elapsed_seconds > max_duration:
                # Apply the ``in_progress → abandoned`` transition via
                # the pure state machine, not an ad-hoc assignment,
                # so Property 12 stays in force for this path too.
                current_domain = DomainGameStatus(row.status.value)
                outcome = transition(current_domain, GameEvent.ABANDON)
                assert isinstance(outcome, TransitionOk), (
                    "in_progress + ABANDON must yield TransitionOk"
                )
                row.status = GameStatus(outcome.new_status.value)
                row.ended_at = now
                session.commit()

                return CompleteGameTimeout(
                    game_id=game_id,
                    ended_at=now,
                    elapsed_seconds=elapsed_seconds,
                )

            # Step 7: happy path — delegate to Scoring_Service.
            score_result = self._scoring_service.compute_and_persist(
                session=session,
                game=row,
                typed_text=typed_text,
                ended_at=now,
            )
            if isinstance(score_result, GameNotEligible):
                # The Scoring_Service re-verified the same invariants
                # we checked above; if it refuses, something changed
                # between the two checks (concurrent writer, corrupted
                # row). Surface as a state mismatch; roll back implicitly
                # by returning before commit — the session context
                # manager will handle cleanup.
                return GameNotInProgress(
                    game_id=game_id,
                    current_status=score_result.current_status,
                )
            if isinstance(score_result, ScoreAlreadyExists):
                # Another writer beat us to it. Surface as a state
                # mismatch rather than an IntegrityError. We don't
                # read the Game's current status here because the
                # concurrent writer would have moved it to COMPLETED;
                # report COMPLETED so the client sees a coherent
                # picture.
                return GameNotInProgress(
                    game_id=game_id,
                    current_status=GameStatus.COMPLETED,
                )

            assert isinstance(score_result, RecordScoreSuccess)

            # Step 8: commit the Score + Game update atomically.
            session.commit()

            # Step 9: success payload.
            return CompleteGameSuccess(
                game_id=score_result.game_id,
                player_id=score_result.player_id,
                score_id=score_result.score_id,
                wpm=score_result.wpm,
                accuracy=score_result.accuracy,
                points=score_result.points,
                ended_at=score_result.ended_at,
                elapsed_seconds=score_result.elapsed_seconds,
            )

    # ------------------------------------------------------------------
    # sweep_timeouts (task 4.5)
    # ------------------------------------------------------------------

    def sweep_timeouts(
        self,
        now: datetime | None = None,
    ) -> list[SweptGame]:
        """Transition every timed-out ``in_progress`` Game to ``abandoned``.

        Called periodically by the async loop in
        :mod:`app.services.timeout_sweeper`. The method is synchronous
        and idempotent: repeated invocations with no new timeouts
        return an empty list and touch no rows.

        A Game is considered timed out when all of the following hold
        (Requirements 9.1 / 9.4 / Property 14):

        * ``status == in_progress``.
        * ``started_at IS NOT NULL``. A NULL ``started_at`` on an
          ``in_progress`` row indicates a corrupted row that
          :meth:`complete` already refuses to score; the sweeper
          leaves such rows untouched so an operator can notice them.
        * ``started_at < now - max_game_duration_seconds``.

        For each matching row the method:

        1. Applies the ``in_progress → abandoned`` transition via
           the pure state machine
           (:func:`app.domain.game_state.transition`) — not an
           ad-hoc assignment — so Property 12 covers this path too.
        2. Writes ``ended_at = now``.
        3. Commits the transaction (a single commit for the whole
           batch, not one per row, so the sweep is atomic from the
           DB's point of view).

        The complementary timeout check inside
        :meth:`GameService.complete` (task 4.4) remains in place:
        even if the sweeper has not yet run, a late result
        submission is still rejected. The sweeper exists so that
        players who never submit (Requirement 9.4) also see their
        rows reach a terminal status in bounded time.

        Args:
            now: The server clock to use as the cutoff. Defaults to
                ``self._clock()``. Injected for tests and for callers
                that want to align sweeper ticks with an externally
                observed clock.

        Returns:
            A list of :class:`SweptGame` values, one per Game that
            the sweep transitioned. The list is empty when nothing
            was due. The order is the order in which the DB returned
            the rows; callers that care about a stable order should
            sort on ``game_id``.
        """
        effective_now = now if now is not None else self._clock()
        max_duration = float(self._settings.max_game_duration_seconds)
        cutoff = effective_now - timedelta(seconds=max_duration)

        swept: list[SweptGame] = []

        with self._session_factory() as session:
            # Select candidate rows up front so we can write the same
            # ``ended_at`` to each and return a consistent payload
            # without re-reading. The ``started_at IS NOT NULL`` guard
            # in the WHERE clause is expressed via SQLAlchemy's native
            # ``is_not`` so a corrupted row with a NULL ``started_at``
            # on an ``in_progress`` status does not participate.
            stmt = select(Game).where(
                Game.status == GameStatus.IN_PROGRESS,
                Game.started_at.is_not(None),
                Game.started_at < cutoff,
            )
            rows = list(session.execute(stmt).scalars())

            for row in rows:
                # Apply the transition through the pure state machine
                # rather than hard-coding the target status — this
                # mirrors what ``complete`` does on its timeout branch
                # and keeps Property 12 in force.
                current_domain = DomainGameStatus(row.status.value)
                outcome = transition(current_domain, GameEvent.ABANDON)
                # ``in_progress + ABANDON`` is an allowed transition per
                # the table in ``game_state``; if this assertion ever
                # fires the table has drifted out of sync with the
                # sweep's WHERE clause.
                assert isinstance(outcome, TransitionOk), (
                    "in_progress + ABANDON must yield TransitionOk"
                )
                new_status = GameStatus(outcome.new_status.value)

                # Normalize ``started_at`` for the returned payload —
                # SQLite's ``DateTime(timezone=True)`` round-trips
                # naive, so we attach UTC the same way ``complete`` does.
                started_at = row.started_at
                assert started_at is not None  # WHERE clause guarantees it
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)

                row.status = new_status
                row.ended_at = effective_now

                swept.append(
                    SweptGame(
                        game_id=row.id,
                        player_id=row.player_id,
                        started_at=started_at,
                        ended_at=effective_now,
                    )
                )

            if swept:
                session.commit()

        return swept


__all__ = [
    "BeginTypingResult",
    "BeginTypingSuccess",
    "CompleteGameResult",
    "CompleteGameSuccess",
    "CompleteGameTimeout",
    "CreateGameResult",
    "CreateGameSuccess",
    "GameAlreadyInProgress",
    "GameNotFound",
    "GameNotInPending",
    "GameNotInProgress",
    "GameService",
    "PlayerNotFound",
    "SweptGame",
]
