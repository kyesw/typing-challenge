/**
 * Results page (route ``/results/:gameId``) — task 12.5.
 *
 * Responsibilities:
 *   * Display the player's ``wpm``, ``accuracy``, ``points``, and
 *     ``rank`` from the result hand-off the Play page persisted in
 *     sessionStorage (:func:`readResultHandoff`)
 *     (Requirements 4.6, 4.7).
 *   * Greet the player by the nickname stored in ``localStorage``
 *     under :const:`NICKNAME_KEY` when available.
 *   * Provide a Play again action that navigates back to ``/ready``.
 *   * Provide a secondary link to ``/dashboard`` so the player can
 *     watch the live leaderboard.
 *   * When the hand-off is missing (e.g. direct navigation or a
 *     refreshed tab without the prior state), render an inline
 *     error with the same Play again action so the player isn't
 *     stuck on a blank page.
 *
 * Safe rendering: every user- or server-supplied string is
 * interpolated through React's default text rendering; no
 * ``dangerouslySetInnerHTML`` is used anywhere (Requirements 13.1,
 * 13.2).
 */

import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { NICKNAME_KEY } from "../api/client";
import { readResultHandoff, type ResultHandoff } from "./PlayPage";

// ---------------------------------------------------------------------------
// Display formatting helpers
// ---------------------------------------------------------------------------

/**
 * Format ``wpm`` for display. WPM is a float; one decimal place
 * is enough precision for a lounge readout while still showing
 * sub-unit differences between close scores.
 */
export function formatWpm(wpm: number): string {
  if (!Number.isFinite(wpm)) return "0.0";
  return wpm.toFixed(1);
}

/**
 * Format ``accuracy`` as a percentage. The backend guarantees
 * accuracy is in ``[0, 100]`` (Requirement 4.2) so we simply round
 * to one decimal place.
 */
export function formatAccuracy(accuracy: number): string {
  if (!Number.isFinite(accuracy)) return "0.0%";
  return `${accuracy.toFixed(1)}%`;
}

/** Format an integer-like score without any trailing decimals. */
export function formatPoints(points: number): string {
  if (!Number.isFinite(points)) return "0";
  return String(Math.trunc(points));
}

/** Format rank as ``#N`` with thousands separators for readability. */
export function formatRank(rank: number): string {
  if (!Number.isFinite(rank) || rank <= 0) return "#—";
  return `#${Math.trunc(rank).toLocaleString()}`;
}

// ---------------------------------------------------------------------------
// Nickname lookup
// ---------------------------------------------------------------------------

/**
 * Read the locally stored nickname (set by :func:`setSession`). Returns
 * ``null`` when no session exists or storage access fails. The lookup
 * is wrapped in a try/catch because ``localStorage`` can throw in
 * private-browsing contexts.
 */
export function readStoredNickname(): string | null {
  try {
    const value = window.localStorage.getItem(NICKNAME_KEY);
    return value !== null && value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ResultsPage(): JSX.Element {
  const { gameId } = useParams<{ gameId: string }>();
  const navigate = useNavigate();

  // Resolve the hand-off once per render keyed by gameId. sessionStorage
  // reads are cheap but wrapping in ``useMemo`` keeps the render function
  // pure-looking and avoids re-parsing on unrelated re-renders.
  const handoff: ResultHandoff | null = useMemo(
    () => (gameId !== undefined ? readResultHandoff(gameId) : null),
    [gameId],
  );

  const nickname = useMemo(() => readStoredNickname(), []);

  if (gameId === undefined) {
    // react-router won't actually mount this page without a gameId,
    // but guard defensively so TypeScript can narrow below.
    return <MissingResult onPlayAgain={() => navigate("/")} />;
  }

  if (handoff === null) {
    return <MissingResult onPlayAgain={() => navigate("/")} />;
  }

  return (
    <main>
      <h1>Results</h1>
      {nickname !== null ? (
        <p data-testid="results-nickname">
          Nice run, <strong>{nickname}</strong>!
        </p>
      ) : null}

      <dl data-testid="results-stats">
        <div>
          <dt>WPM</dt>
          <dd data-testid="results-wpm">{formatWpm(handoff.wpm)}</dd>
        </div>
        <div>
          <dt>Accuracy</dt>
          <dd data-testid="results-accuracy">
            {formatAccuracy(handoff.accuracy)}
          </dd>
        </div>
        <div>
          <dt>Points</dt>
          <dd data-testid="results-points">{formatPoints(handoff.points)}</dd>
        </div>
        <div>
          <dt>Rank</dt>
          <dd data-testid="results-rank">{formatRank(handoff.rank)}</dd>
        </div>
      </dl>

      <nav aria-label="After-game actions">
        <button
          type="button"
          onClick={() => navigate("/")}
          data-testid="results-play-again"
        >
          Exit
        </button>
      </nav>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Missing hand-off fallback
// ---------------------------------------------------------------------------

interface MissingResultProps {
  onPlayAgain: () => void;
}

function MissingResult({ onPlayAgain }: MissingResultProps): JSX.Element {
  return (
    <main>
      <h1>Results unavailable</h1>
      <p role="alert" data-testid="results-missing">
        We couldn't find the score for this game. The result may have
        been cleared when the tab was refreshed.
      </p>
      <nav aria-label="After-game actions">
        <button
          type="button"
          onClick={onPlayAgain}
          data-testid="results-play-again"
        >
          Exit
        </button>
      </nav>
    </main>
  );
}
