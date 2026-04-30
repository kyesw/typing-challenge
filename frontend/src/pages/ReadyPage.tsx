/**
 * Ready page (route ``/ready``) — task 12.3.
 *
 * Responsibilities:
 *   * Render a Start button (Requirement 2.1).
 *   * On click, call ``POST /games`` (Requirement 2.2).
 *   * On 201, stash the Game's prompt + ``startAt`` in session
 *     storage (keyed by ``gameId``) so the Countdown + Typing page
 *     (task 12.4) can read them without another round-trip, then
 *     navigate to ``/play/:gameId`` (Requirement 2.5).
 *   * On error, show an inline error message without navigating.
 *
 * Only safe text interpolation is used anywhere that renders
 * user- or server-supplied strings (Requirement 13.1 / 13.2); no
 * ``dangerouslySetInnerHTML``.
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiRequestError,
  startGame,
  type StartGameResponse,
} from "../api/client";

// ---------------------------------------------------------------------------
// Cross-route hand-off: prompt + startAt for the /play page.
// ---------------------------------------------------------------------------

/**
 * sessionStorage key prefix for the prompt + startAt payload the
 * Ready page hands off to the Countdown + Typing page. Keyed by
 * ``gameId`` so a player who somehow navigates to two different
 * games in one session doesn't end up with stale data.
 *
 * sessionStorage (rather than localStorage) is used deliberately:
 * the hand-off only needs to live for the current tab, and it gets
 * discarded on tab close so a new session starts clean.
 */
export const GAME_HANDOFF_KEY_PREFIX = "typing-game.gameHandoff.";

/** Payload the /ready page writes and the /play page reads. */
export interface GameHandoff {
  gameId: string;
  promptId: string;
  prompt: string;
  language: string;
  status: string;
  startAt: string;
}

/** Persist the hand-off for ``gameId``. Best-effort; ignores failures. */
export function persistGameHandoff(payload: GameHandoff): void {
  try {
    window.sessionStorage.setItem(
      `${GAME_HANDOFF_KEY_PREFIX}${payload.gameId}`,
      JSON.stringify(payload),
    );
  } catch {
    // sessionStorage can throw (e.g. quota, private browsing).
    // The /play page can always fall back to GET /games/{gameId}.
  }
}

/** Read back the hand-off for ``gameId``; returns ``null`` if absent. */
export function readGameHandoff(gameId: string): GameHandoff | null {
  try {
    const raw = window.sessionStorage.getItem(
      `${GAME_HANDOFF_KEY_PREFIX}${gameId}`,
    );
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as GameHandoff).gameId === "string" &&
      typeof (parsed as GameHandoff).prompt === "string" &&
      typeof (parsed as GameHandoff).startAt === "string"
    ) {
      return parsed as GameHandoff;
    }
    return null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type ReadyState =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "error"; message: string };

export function ReadyPage(): JSX.Element {
  const navigate = useNavigate();
  const [state, setState] = useState<ReadyState>({ kind: "idle" });

  async function handleStart(): Promise<void> {
    if (state.kind === "starting") return;
    setState({ kind: "starting" });

    try {
      const result: StartGameResponse = await startGame();
      persistGameHandoff({
        gameId: result.gameId,
        promptId: result.promptId,
        prompt: result.prompt,
        language: result.language,
        status: result.status,
        startAt: result.startAt,
      });
      navigate(`/play/${result.gameId}`);
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setState({ kind: "error", message: err.error.message });
        return;
      }
      setState({
        kind: "error",
        message:
          "Unable to reach the server. Check your connection and try again.",
      });
    }
  }

  const starting = state.kind === "starting";

  return (
    <main>
      <h1>Ready?</h1>
      <p data-testid="ready-placeholder">Press Start when you are ready.</p>
      <button
        type="button"
        onClick={() => void handleStart()}
        disabled={starting}
        data-testid="ready-start"
      >
        {starting ? "Starting…" : "Start"}
      </button>

      {state.kind === "error" ? (
        <p role="alert" data-testid="ready-error">
          {state.message}
        </p>
      ) : null}
    </main>
  );
}
