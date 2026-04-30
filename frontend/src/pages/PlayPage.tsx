/**
 * Countdown + Typing page (route ``/play/:gameId``) — task 12.4.
 *
 * Responsibilities:
 *   * Read the hand-off payload the Ready page wrote to
 *     ``sessionStorage`` (``GameHandoff``) for the prompt + startAt.
 *     Fall back to ``GET /games/{gameId}`` when the hand-off is
 *     absent (e.g. the player refreshed the tab mid-flow).
 *   * Render a 3→2→1 countdown, one second per tick
 *     (Requirement 3.1).
 *   * When the countdown ends, call ``POST /games/{gameId}/begin`` to
 *     transition the Game to ``in_progress`` (Requirement 3.2) and
 *     reveal the prompt + typing input.
 *   * Capture keystrokes locally and render each typed character as
 *     "correct" or "incorrect" relative to the prompt, without any
 *     per-keystroke server call (Requirements 3.3, 3.4).
 *   * When the player finishes (typed length === prompt length), call
 *     ``POST /games/{gameId}/result`` with the typed text and the
 *     client-observed elapsed time in ms (server ignores for scoring,
 *     Requirement 3.5 / 3.6). Stash the response for the Results page
 *     and navigate to ``/results/:gameId``.
 *   * When the server rejects the submission with the ``game_timeout``
 *     code, surface a "time's up" message and auto-navigate back to
 *     ``/ready`` after :const:`TIMEOUT_REDIRECT_MS` so the player isn't
 *     stranded on the timeout screen; a manual "Back to Ready" button
 *     provides an immediate escape hatch (Requirement 9.3, task 12.6).
 *   * When the submission ``fetch`` rejects at the network layer
 *     (Requirement 10.2, task 12.7), buffer ``(typedText, elapsedMs)``
 *     locally and enter a ``retrying`` state. A reconnect is detected
 *     either by the browser's ``online`` event or by a bounded
 *     exponential-backoff timer (so players whose browser never fires
 *     ``online`` — e.g., a flaky VPN — still get retried). On a
 *     successful retry the result flows into the normal /results page
 *     (Requirement 10.3). If the retry surfaces a server response
 *     indicating the Game has already been transitioned to
 *     ``abandoned`` (409 ``game_conflict`` with
 *     ``details.currentStatus === "abandoned"``, or 409
 *     ``game_timeout``), the page enters an ``abandoned`` state that
 *     shows an error and offers a "New game" button back to
 *     ``/ready`` (Requirement 10.4). The local typing timer is never
 *     paused on disconnect because it is already client-local — it
 *     uses ``performance.now()`` with no network dependency — so
 *     Requirement 10.1 is satisfied by construction.
 *
 * Safe rendering: every server- or user-supplied string is
 * interpolated through React's default text rendering; no
 * ``dangerouslySetInnerHTML`` is used anywhere (Requirements 13.1,
 * 13.2).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  ApiRequestError,
  beginGame,
  getGame,
  submitResult,
  type BeginGameResponse,
  type SubmitResultResponse,
} from "../api/client";
import { readGameHandoff } from "./ReadyPage";

// Stylesheet for the four Character_State visual treatments, the
// cursor indicator, and the aggregate typo indicator
// (typing-input-highlighting Requirements 2.1–2.4, 3.1, 4.2, 7.1;
// task 3.1). Vite inlines the CSS at build time; the test runner
// has ``css: false`` in ``vite.config.ts`` so this import is a no-op
// under jsdom.
import "./PlayPage.css";

// ---------------------------------------------------------------------------
// Cross-route hand-off for the Results page
// ---------------------------------------------------------------------------

/**
 * Milliseconds the timeout screen stays up before the page auto-navigates
 * back to ``/ready`` (Requirement 9.3, task 12.6). Kept short enough that
 * the player isn't stranded, long enough that the "Time's up" message is
 * readable. The manual "Back to Ready" button fires immediately.
 */
export const TIMEOUT_REDIRECT_MS = 3000;

/**
 * Retry backoff schedule (in ms) for a buffered result submission after a
 * network failure (Requirement 10.2, task 12.7). The page still retries
 * immediately on the browser's ``online`` event; this fallback covers
 * browsers that don't fire it (or fire it unreliably). The schedule is
 * bounded so a dead network doesn't spin retries forever — once the
 * schedule is exhausted the last entry is reused for subsequent attempts
 * (effectively a steady-state retry every 10s).
 *
 * The values are intentionally small relative to the game's Maximum_Game_Duration
 * so a player whose network blip resolves quickly still lands within the
 * server-side window.
 */
export const RETRY_BACKOFF_MS: readonly number[] = [
  2000, 4000, 6000, 10000,
] as const;

/**
 * sessionStorage key prefix for the score payload the Play page
 * hands off to the Results page (task 12.5 reads this). Keyed by
 * ``gameId`` so the Results page can look up its own result
 * without having to re-fetch from the server.
 */
export const RESULT_HANDOFF_KEY_PREFIX = "typing-game.resultHandoff.";

/**
 * Payload persisted after a successful ``POST /games/{id}/result``.
 * Mirrors :class:`SubmitResultResponse` plus the elapsed time the
 * client observed (useful for post-game display; optional).
 */
export interface ResultHandoff {
  gameId: string;
  wpm: number;
  accuracy: number;
  points: number;
  rank: number;
  endedAt: string;
  clientElapsedMs: number;
}

/** Persist the hand-off for ``gameId``. Best-effort; ignores failures. */
export function persistResultHandoff(payload: ResultHandoff): void {
  try {
    window.sessionStorage.setItem(
      `${RESULT_HANDOFF_KEY_PREFIX}${payload.gameId}`,
      JSON.stringify(payload),
    );
  } catch {
    // sessionStorage can throw (quota, private browsing). The
    // Results page can always fall back to showing just the gameId.
  }
}

/** Read back the hand-off for ``gameId``; returns ``null`` if absent. */
export function readResultHandoff(gameId: string): ResultHandoff | null {
  try {
    const raw = window.sessionStorage.getItem(
      `${RESULT_HANDOFF_KEY_PREFIX}${gameId}`,
    );
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as ResultHandoff).gameId === "string" &&
      typeof (parsed as ResultHandoff).wpm === "number" &&
      typeof (parsed as ResultHandoff).accuracy === "number" &&
      typeof (parsed as ResultHandoff).points === "number" &&
      typeof (parsed as ResultHandoff).rank === "number"
    ) {
      return parsed as ResultHandoff;
    }
    return null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Character-by-character feedback
// ---------------------------------------------------------------------------

/**
 * Four-valued classification of a Prompt_Character relative to the
 * current Typed_Text, used by the ``classifyView`` pure helper and by
 * the ``TypingView`` renderer (typing-input-highlighting design,
 * Model 1). Exactly one value is assigned per Character_Display
 * (Requirement 1.7):
 *
 *   * ``pending``   — the index is at or beyond the Cursor_Position
 *                     and not the cursor itself.
 *   * ``correct``   — the index is before the Cursor_Position and the
 *                     typed character matches the prompt character.
 *   * ``incorrect`` — the index is before the Cursor_Position and the
 *                     typed character does not match the prompt
 *                     character (a Typo).
 *   * ``current``   — the index equals the Cursor_Position and the
 *                     cursor has not yet moved past the end of the
 *                     prompt.
 */
export type CharacterState =
  | "pending"
  | "correct"
  | "incorrect"
  | "current";

/**
 * Result of a single ``classifyView(prompt, typed)`` call. Bundles the
 * per-index Character_State array with the scalar Cursor_Position so
 * the renderer can derive everything it needs from a single memoized
 * value (design Model 2).
 *
 * Invariants (enforced by ``classifyView`` and checked by property
 * tests in task 1.3 / 1.4):
 *   * ``states.length === prompt.length``
 *   * ``cursorIndex === Math.min(typed.length, prompt.length)``
 */
export interface ClassifyResult {
  /** One Character_State per Prompt_Character, in prompt order. */
  states: readonly CharacterState[];
  /** ``min(len(typed), len(prompt))`` — the Cursor_Position. */
  cursorIndex: number;
}

/**
 * Compute the full per-character classification for a ``(prompt, typed)``
 * snapshot in a single pass (typing-input-highlighting design, Property 1
 * and Model 2 invariants).
 *
 * For each index ``i`` in ``[0, prompt.length)`` the decision table is:
 *
 *   * ``"current"``   if ``i === len(typed)`` and ``i < len(prompt)``
 *   * ``"correct"``   if ``i < len(typed)`` and ``typed[i] === prompt[i]``
 *   * ``"incorrect"`` if ``i < len(typed)`` and ``typed[i] !== prompt[i]``
 *   * ``"pending"``   otherwise (``i > len(typed)``, or
 *                     ``i === len(typed)`` when the cursor has moved past
 *                     the end of the prompt)
 *
 * The same loop classifies each character in a single pass.
 *
 * ``cursorIndex`` is clamped to ``[0, prompt.length]`` via ``Math.min`` so
 * callers that accidentally pass ``typed`` longer than ``prompt`` still
 * get a safe, in-range Cursor_Position. This mirrors the onChange cap in
 * the component (Requirements 5.1, 5.2) and ensures the classifier never
 * indexes past the prompt.
 */
export function classifyView(
  prompt: string,
  typed: string,
): ClassifyResult {
  const promptLength = prompt.length;
  const typedLength = typed.length;
  // ``min`` clamps the defensive case where ``typed`` is longer than the
  // prompt; the loop only ever reads ``prompt[i]`` and ``typed[i]`` for
  // ``i < promptLength``, so nothing is read past the prompt.
  const cursorIndex = Math.min(typedLength, promptLength);

  const states: CharacterState[] = new Array(promptLength);

  for (let i = 0; i < promptLength; i += 1) {
    if (i < typedLength) {
      if (typed[i] === prompt[i]) {
        states[i] = "correct";
      } else {
        states[i] = "incorrect";
      }
    } else if (i === typedLength) {
      // ``i === typedLength`` and ``i < promptLength`` (the loop
      // condition) — this is the Cursor_Position.
      states[i] = "current";
    } else {
      states[i] = "pending";
    }
  }

  return { states, cursorIndex };
}

// ---------------------------------------------------------------------------
// Retry-path helpers
// ---------------------------------------------------------------------------

/**
 * Inspect an :class:`ApiRequestError` from ``POST /games/{id}/result``
 * to decide whether it represents the "Game is already abandoned"
 * outcome the retry path cares about (Requirement 10.4).
 *
 * Two backend shapes collapse to "abandoned":
 *   * 409 ``game_conflict`` with ``details.currentStatus === "abandoned"``
 *     — the Game was swept out-of-band (task 4.5) before the buffered
 *     submission landed.
 *   * 409 ``game_timeout`` — the ``complete`` path itself detected the
 *     over-duration submission.
 *
 * ``game_timeout`` is handled by its dedicated callsite in the
 * retry effect, so this helper only tests the conflict-with-abandoned
 * case. Keeping it as a small named predicate makes the intent
 * obvious at the callsite and makes the branching easy to unit-test.
 */
export function isAbandonedConflict(err: ApiRequestError): boolean {
  if (err.error.code !== "game_conflict") return false;
  const details = err.error.details;
  if (details === undefined || details === null) return false;
  return details.currentStatus === "abandoned";
}

// ---------------------------------------------------------------------------
// Page state machine
// ---------------------------------------------------------------------------

/**
 * High-level states the page cycles through. Keeping them as a
 * discriminated union makes the JSX branches unambiguous and lets
 * TypeScript flag missing transitions.
 */
type PageState =
  | { kind: "loading" } // fetching prompt from handoff / API
  | { kind: "countdown"; prompt: string; count: 3 | 2 | 1 }
  | { kind: "beginning"; prompt: string } // POST /begin in flight
  | {
      kind: "typing";
      prompt: string;
      typed: string;
      startedAtMs: number;
      startedAtServer: string;
    }
  | { kind: "submitting"; prompt: string; typed: string; elapsedMs: number }
  | {
      // POST /result failed at the network layer (fetch rejected) and
      // we're holding onto the buffered submission, waiting for
      // connectivity so we can retry (Requirement 10.2, task 12.7).
      // ``attempt`` starts at 1 on first entry and increments per
      // retry; it indexes into :const:`RETRY_BACKOFF_MS`.
      kind: "retrying";
      prompt: string;
      typed: string;
      elapsedMs: number;
      attempt: number;
    }
  | { kind: "timeout" } // 409 game_timeout — see Requirement 9.3
  | {
      // The buffered submission retried successfully, but the server
      // reported that the Game has already been transitioned to
      // ``abandoned`` (Requirement 10.4, task 12.7). We show an
      // error and offer a "New game" button that routes back to
      // ``/ready``.
      kind: "abandoned";
    }
  | { kind: "error"; message: string };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PlayPage(): JSX.Element {
  const { gameId } = useParams<{ gameId: string }>();
  const navigate = useNavigate();
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const inputRef = useRef<HTMLInputElement>(null);

  // ---- Step 1: load the prompt (handoff first, GET /games fallback) ----
  useEffect(() => {
    if (gameId === undefined) {
      setState({ kind: "error", message: "Missing game id." });
      return;
    }

    let cancelled = false;

    const handoff = readGameHandoff(gameId);
    if (handoff !== null) {
      setState({ kind: "countdown", prompt: handoff.prompt, count: 3 });
      return;
    }

    void (async () => {
      try {
        const game = await getGame(gameId);
        if (cancelled) return;
        setState({ kind: "countdown", prompt: game.prompt, count: 3 });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiRequestError) {
          setState({ kind: "error", message: err.error.message });
        } else {
          setState({
            kind: "error",
            message:
              "Unable to load the prompt. Check your connection and try again.",
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [gameId]);

  // ---- Step 2: drive the 3→2→1 countdown via setTimeout ----
  useEffect(() => {
    if (state.kind !== "countdown") return;

    const handle = window.setTimeout(() => {
      setState((current) => {
        if (current.kind !== "countdown") return current;
        if (current.count > 1) {
          return {
            kind: "countdown",
            prompt: current.prompt,
            count: (current.count - 1) as 2 | 1,
          };
        }
        // Countdown just hit 1 → advance to the "beginning" phase so
        // the begin request can fire from a dedicated effect.
        return { kind: "beginning", prompt: current.prompt };
      });
    }, 1000);

    return () => {
      window.clearTimeout(handle);
    };
    // We intentionally depend on ``state`` so the timer restarts at
    // every tick. The ticking effect is the only thing that advances
    // ``state.count`` or transitions into ``beginning``.
  }, [state]);

  // ---- Step 3: POST /begin, then flip to typing ----
  useEffect(() => {
    if (state.kind !== "beginning") return;
    if (gameId === undefined) return;

    let cancelled = false;

    void (async () => {
      try {
        const begin: BeginGameResponse = await beginGame(gameId);
        if (cancelled) return;
        setState({
          kind: "typing",
          prompt: state.prompt,
          typed: "",
          startedAtMs:
            typeof performance !== "undefined"
              ? performance.now()
              : Date.now(),
          startedAtServer: begin.startedAt,
        });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiRequestError) {
          // Timeout can happen here in theory if the sweeper raced
          // us between /games and /begin; surface the timeout UI so
          // task 12.6 can route the player back to /ready.
          if (err.error.code === "game_timeout") {
            setState({ kind: "timeout" });
            return;
          }
          setState({ kind: "error", message: err.error.message });
          return;
        }
        setState({
          kind: "error",
          message:
            "Unable to start typing. Check your connection and try again.",
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state, gameId]);

  // ---- Step 4: focus the input when typing starts ----
  useEffect(() => {
    if (state.kind === "typing") {
      inputRef.current?.focus();
    }
  }, [state.kind]);

  // ---- Auto-navigate back to /ready after a timeout (Requirement 9.3) ----
  // The manual "Back to Ready" button below is the immediate escape
  // hatch; this timer guarantees the player isn't stranded on the
  // timeout screen if they don't click anything. ``state.kind`` is
  // the only dependency — we want exactly one timer per entry into
  // the ``timeout`` state.
  useEffect(() => {
    if (state.kind !== "timeout") return;

    const handle = window.setTimeout(() => {
      navigate("/ready");
    }, TIMEOUT_REDIRECT_MS);

    return () => {
      window.clearTimeout(handle);
    };
  }, [state.kind, navigate]);

  // ---- Step 5: submit on completion ----
  useEffect(() => {
    if (state.kind !== "submitting") return;
    if (gameId === undefined) return;

    let cancelled = false;

    void (async () => {
      try {
        const result: SubmitResultResponse = await submitResult(
          gameId,
          state.typed,
          state.elapsedMs,
        );
        if (cancelled) return;

        persistResultHandoff({
          gameId: result.gameId,
          wpm: result.wpm,
          accuracy: result.accuracy,
          points: result.points,
          rank: result.rank,
          endedAt: result.endedAt,
          clientElapsedMs: state.elapsedMs,
        });
        navigate(`/results/${result.gameId}`);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiRequestError) {
          if (err.error.code === "game_timeout") {
            setState({ kind: "timeout" });
            return;
          }
          if (isAbandonedConflict(err)) {
            // The Game was already abandoned (e.g., sweeper ran
            // while the client was offline). Treat like the retry
            // path's abandoned outcome — show an error and offer
            // a new game (Requirement 10.4).
            setState({ kind: "abandoned" });
            return;
          }
          setState({ kind: "error", message: err.error.message });
          return;
        }
        // Network-layer failure (``fetch`` rejected). Buffer the
        // submission and enter the retry state (Requirement 10.2,
        // task 12.7). The local typing timer is already frozen in
        // ``elapsedMs``; we do not need to restart anything.
        setState({
          kind: "retrying",
          prompt: state.prompt,
          typed: state.typed,
          elapsedMs: state.elapsedMs,
          attempt: 1,
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state, gameId, navigate]);

  // ---- Step 5b: retry a buffered submission (Requirement 10.2 / 10.3 / 10.4)
  // Two triggers:
  //   * the browser's ``online`` event (fires when connectivity is
  //     restored), and
  //   * a fallback backoff timer indexed by ``attempt`` into
  //     :const:`RETRY_BACKOFF_MS`.
  // Whichever fires first issues the retry; both de-dupe via the
  // ``retryInFlight`` guard so a near-simultaneous ``online`` event
  // and timer tick don't submit twice. The effect re-subscribes on
  // every ``retrying`` attempt, so each attempt gets a fresh pair
  // of triggers.
  useEffect(() => {
    if (state.kind !== "retrying") return;
    if (gameId === undefined) return;

    let cancelled = false;
    let retryInFlight = false;

    const retry = async (): Promise<void> => {
      if (cancelled || retryInFlight) return;
      retryInFlight = true;
      try {
        const result: SubmitResultResponse = await submitResult(
          gameId,
          state.typed,
          state.elapsedMs,
        );
        if (cancelled) return;

        persistResultHandoff({
          gameId: result.gameId,
          wpm: result.wpm,
          accuracy: result.accuracy,
          points: result.points,
          rank: result.rank,
          endedAt: result.endedAt,
          clientElapsedMs: state.elapsedMs,
        });
        navigate(`/results/${result.gameId}`);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiRequestError) {
          // Server responded: the retry window is over. Figure out
          // which terminal UI to show.
          if (
            err.error.code === "game_timeout" ||
            isAbandonedConflict(err)
          ) {
            setState({ kind: "abandoned" });
            return;
          }
          setState({ kind: "error", message: err.error.message });
          return;
        }
        // Still a network failure — schedule another attempt by
        // bumping the counter. The effect's dependency on
        // ``state.attempt`` re-runs us with a fresh backoff.
        retryInFlight = false;
        setState((current) => {
          if (current.kind !== "retrying") return current;
          return { ...current, attempt: current.attempt + 1 };
        });
      }
    };

    // Trigger 1: connectivity-restored event.
    const handleOnline = (): void => {
      void retry();
    };
    window.addEventListener("online", handleOnline);

    // Trigger 2: backoff timer. Cap ``attempt`` at the last schedule
    // slot so we keep retrying at a steady ~10s cadence once the
    // backoff is exhausted.
    const slot = Math.min(state.attempt - 1, RETRY_BACKOFF_MS.length - 1);
    const delay = RETRY_BACKOFF_MS[slot]!;
    const timerHandle = window.setTimeout(() => {
      void retry();
    }, delay);

    return () => {
      cancelled = true;
      window.removeEventListener("online", handleOnline);
      window.clearTimeout(timerHandle);
    };
  }, [state, gameId, navigate]);

  // ---- Keystroke handler ----
  const handleInput = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>): void => {
      setState((current) => {
        if (current.kind !== "typing") return current;

        // Cap typed length at the prompt length — extra characters are
        // dropped on the floor rather than highlighted. This keeps the
        // feedback mapping tight and sidesteps the backend's typed-text
        // length guard (task 9.4 / Requirement 13.2 of the base spec).
        // It is also the onChange-side enforcement of the
        // typing-input-highlighting Requirements 5.1 / 5.2 that the
        // Typed_Text never exceeds the Prompt length, and is the
        // behavior covered by Property 6 in that feature's design.
        const raw = event.target.value;
        const capped = raw.slice(0, current.prompt.length);

        // If the player just completed the prompt, transition into
        // "submitting" so the dedicated effect fires the POST.
        if (capped.length === current.prompt.length) {
          const elapsedMs =
            (typeof performance !== "undefined"
              ? performance.now()
              : Date.now()) - current.startedAtMs;
          return {
            kind: "submitting",
            prompt: current.prompt,
            typed: capped,
            elapsedMs,
          };
        }

        return { ...current, typed: capped };
      });
    },
    [],
  );

  // ---- Render ----

  if (state.kind === "loading") {
    return (
      <main>
        <h1>Loading…</h1>
        <p data-testid="play-loading">Fetching your prompt.</p>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main>
        <h1>Something went wrong</h1>
        <p role="alert" data-testid="play-error">
          {state.message}
        </p>
        <button
          type="button"
          onClick={() => navigate("/ready")}
          data-testid="play-error-back"
        >
          Back to Ready
        </button>
      </main>
    );
  }

  if (state.kind === "timeout") {
    return (
      <main>
        <h1>Time's up</h1>
        <p data-testid="play-timeout">
          The game exceeded the maximum duration.
        </p>
        <button
          type="button"
          onClick={() => navigate("/ready")}
          data-testid="play-timeout-back"
        >
          Back to Ready
        </button>
      </main>
    );
  }

  if (state.kind === "countdown") {
    return (
      <main>
        <h1>Get ready</h1>
        <p data-testid="play-countdown" aria-live="polite">
          {state.count}
        </p>
      </main>
    );
  }

  if (state.kind === "beginning") {
    return (
      <main>
        <h1>Starting…</h1>
        <p data-testid="play-beginning">Starting the typing phase.</p>
      </main>
    );
  }

  if (state.kind === "submitting") {
    return (
      <main>
        <h1>Scoring…</h1>
        <p data-testid="play-submitting">Submitting your result.</p>
      </main>
    );
  }

  if (state.kind === "retrying") {
    return (
      <main>
        <h1>Reconnecting…</h1>
        <p role="status" aria-live="polite" data-testid="play-retrying">
          Network issue — we'll submit your result as soon as the
          connection is back.
        </p>
      </main>
    );
  }

  if (state.kind === "abandoned") {
    return (
      <main>
        <h1>Game was abandoned</h1>
        <p role="alert" data-testid="play-abandoned">
          The game ended while you were offline, so your result couldn't be
          recorded. Start a new game to try again.
        </p>
        <button
          type="button"
          onClick={() => navigate("/ready")}
          data-testid="play-abandoned-new-game"
        >
          New game
        </button>
      </main>
    );
  }

  // state.kind === "typing"
  return <TypingView state={state} onChange={handleInput} inputRef={inputRef} />;
}

// ---------------------------------------------------------------------------
// TypingView — split out so classification runs only when typing.
// ---------------------------------------------------------------------------

/**
 * Shape of the ``typing`` variant of :type:`PageState`. Exported so
 * property-based tests can construct a synthetic ``typing`` snapshot
 * and render :func:`TypingView` directly without driving the full
 * countdown / begin state machine for every iteration
 * (typing-input-highlighting design, "Component property tests"
 * section — "a minimal harness that hosts TypingView with synthetic
 * PageState input").
 */
export type TypingPageState = Extract<PageState, { kind: "typing" }>;

export interface TypingViewProps {
  state: TypingPageState;
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
  inputRef: React.RefObject<HTMLInputElement>;
}

/**
 * Renders the typing surface for the ``typing`` page state. Exported
 * so property-based tests can mount it against synthetic
 * ``(prompt, typed)`` inputs without having to drive the countdown,
 * ``POST /begin``, and keystroke plumbing every iteration (see
 * :file:`TypingView.pbt.test.tsx`).
 */
export function TypingView({
  state,
  onChange,
  inputRef,
}: TypingViewProps): JSX.Element {
  const { states, cursorIndex } = useMemo(
    () => classifyView(state.prompt, state.typed),
    [state.prompt, state.typed],
  );

  return (
    <main>
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
      <p
        data-testid="play-prompt"
        aria-label="Prompt"
        data-cursor-index={cursorIndex}
        onClick={() => inputRef.current?.focus()}
        style={{ cursor: "text" }}
      >
        {/*
          Each character is rendered inside its own <span> so we can
          mark it correct / incorrect / pending / current for visual
          feedback (typing-input-highlighting Requirements 1.1, 2.5,
          2.6, 7.2). ``state.prompt`` is server-supplied but rendered
          via React's default text interpolation — no HTML injection
          surface (Requirement 8.1).
        */}
        {state.prompt.split("").map((ch, i) => (
          <span
            key={i}
            data-testid={`play-char-${i}`}
            data-char-class={states[i]}
            className={`char char--${states[i]}`}
            // Only the Character_Display at the Cursor_Position
            // carries ``aria-current``; everywhere else the
            // attribute is absent (Requirement 7.3 / Property 8).
            aria-current={states[i] === "current" ? "true" : undefined}
          >
            {ch}
          </span>
        ))}
      </p>
      <label htmlFor="play-typing-input" className="sr-only">
        Your typing
        <input
          id="play-typing-input"
          ref={inputRef}
          type="text"
          value={state.typed}
          onChange={onChange}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          maxLength={state.prompt.length}
          data-testid="play-input"
          aria-label="Typing input"
          className="ghost-input"
        />
      </label>
    </main>
  );
}
