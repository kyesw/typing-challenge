"""Async runner that drives :meth:`GameService.sweep_timeouts` on a cadence.

Task 4.5 splits the timeout-enforcement work in two. The decision logic
— pick every ``in_progress`` Game whose ``started_at`` is older than
``now - Maximum_Game_Duration`` and transition it to ``abandoned`` —
lives on :class:`GameService` as a synchronous method
(``sweep_timeouts``). This module wraps it in a small asyncio loop so
FastAPI's lifespan hook can start one background task on application
startup and cancel it on shutdown.

Design notes:

- **Sweeper is synchronous inside the loop.** The service's
  ``sweep_timeouts`` call is a plain blocking DB operation, which is
  fine for SQLite + single-instance deployments. We invoke it directly
  from the async loop; each tick is expected to complete in O(ms) on
  the expected lounge scale (tens of rows at most). If a future
  deployment needs a truly async persistence path, the call can be
  wrapped in :func:`asyncio.to_thread` here without changing the
  service surface.
- **Cancellation is first-class.** ``run`` catches
  :class:`asyncio.CancelledError` and returns cleanly. That way a
  caller can ``task.cancel()`` during shutdown and ``await task``
  without the task surfacing the cancellation as an unhandled
  exception. Any other exception raised by a single sweep tick is
  logged and the loop continues; a transient DB error must not kill
  the sweeper permanently.
- **Clock injection.** The sweeper takes an optional ``clock`` so
  tests can drive it with a deterministic source; in production the
  service's own default clock is used when ``clock`` is ``None``.
- **No singleton.** The app wires a single :class:`TimeoutSweeper`
  instance at startup and owns the resulting :class:`asyncio.Task`;
  this module does not hold module-level state.

Requirements addressed:
- 9.1 (Maximum_Game_Duration enforced on every ``in_progress`` Game)
- 9.4 (An ``in_progress`` Game that exceeds the duration without a
  submission transitions to ``abandoned``)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from .game_service import GameService, SweptGame


logger = logging.getLogger(__name__)


class TimeoutSweeper:
    """Periodic background driver for :meth:`GameService.sweep_timeouts`.

    The sweeper is intentionally minimal: it owns an interval and an
    optional clock, repeatedly asks the service to sweep, and sleeps
    between ticks. The service owns the correctness-critical decision
    logic (which Games are timed out, how the transition is applied);
    this class exists only to schedule calls.
    """

    def __init__(
        self,
        game_service: GameService,
        *,
        interval_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the sweeper.

        Args:
            game_service: The :class:`GameService` instance to drive.
                Typically the same instance the HTTP endpoints use, so
                a timeout triggered by a late submission and a timeout
                triggered by the sweeper go through identical code
                paths.
            interval_seconds: How long to sleep between ticks. Must
                be positive. The FastAPI app supplies this from
                :attr:`Settings.timeout_sweeper_interval_seconds`.
            clock: Optional zero-arg callable returning a
                timezone-aware ``datetime``. Injected for tests; when
                ``None`` the service's own clock is used on every
                tick (by passing ``now=None`` to ``sweep_timeouts``).
        """
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be positive, got {interval_seconds!r}"
            )
        self._game_service = game_service
        self._interval_seconds = interval_seconds
        self._clock = clock

    async def run(self) -> None:
        """Loop forever, sweeping every ``interval_seconds``.

        The loop exits cleanly on :class:`asyncio.CancelledError` so
        callers can cancel the wrapping task during shutdown without
        seeing the cancellation surface as an unhandled exception.
        Any other exception from a single tick is logged and the
        loop continues; a transient DB error must not kill the
        sweeper for the lifetime of the process.
        """
        try:
            while True:
                # A single tick: compute ``now`` (if a clock was
                # injected), ask the service to sweep, and log the
                # result count for operational visibility. The call
                # is synchronous; on SQLite + tens of rows this is a
                # sub-millisecond operation.
                try:
                    now = self._clock() if self._clock is not None else None
                    swept: list[SweptGame] = self._game_service.sweep_timeouts(
                        now=now,
                    )
                except asyncio.CancelledError:
                    # Re-raise so the outer handler exits cleanly.
                    raise
                except Exception:  # pragma: no cover - defensive
                    # A tick failure must not kill the loop. Log and
                    # keep running; the next tick will retry.
                    logger.exception(
                        "TimeoutSweeper tick failed; continuing."
                    )
                else:
                    if swept:
                        logger.info(
                            "TimeoutSweeper abandoned %d game(s): %s",
                            len(swept),
                            ", ".join(g.game_id for g in swept),
                        )

                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            # Clean shutdown. Do not re-raise: callers ``await`` the
            # task and a re-raise would force them to handle it.
            logger.debug("TimeoutSweeper cancelled; exiting loop.")
            return

    async def start(self) -> asyncio.Task[None]:
        """Schedule :meth:`run` on the current event loop and return the task.

        The caller is responsible for retaining the task and passing
        it to :meth:`stop` on shutdown. Kept ``async`` for symmetry
        with :meth:`stop` and because the FastAPI lifespan hook that
        calls this is itself an async context.
        """
        return asyncio.create_task(self.run(), name="timeout-sweeper")

    async def stop(self, task: asyncio.Task[None]) -> None:
        """Cancel ``task`` and await its clean exit.

        The method tolerates a task that has already finished (for
        instance, if :meth:`run` exited because of an unexpected
        error). It never raises :class:`asyncio.CancelledError` to
        the caller — :meth:`run` catches it internally — so app
        shutdown code can call ``await sweeper.stop(task)`` without
        further guarding.
        """
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # ``run`` swallows CancelledError, but if the task was
            # cancelled before it entered the try block we see it
            # here. Treat as a normal shutdown.
            return


__all__ = ["TimeoutSweeper"]
