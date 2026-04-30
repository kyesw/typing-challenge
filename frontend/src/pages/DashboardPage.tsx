/**
 * Dashboard page (route ``/dashboard``) — tasks 13.1, 13.2, 13.3.
 *
 * Responsibilities:
 *   * On mount, fetch ``GET /leaderboard`` once for the initial
 *     snapshot and render it (task 13.1 / Requirement 6.1).
 *   * Render the top-N rows with the required fields — ``nickname``,
 *     ``bestWpm``, ``bestAccuracy``, ``bestPoints`` — using only
 *     React's default text interpolation so untrusted nicknames
 *     cannot inject HTML (task 13.2 / Requirements 6.1, 13.1).
 *   * Poll ``GET /leaderboard`` every 1 second while the route is
 *     mounted, re-rendering from each successful snapshot
 *     (task 13.3 / Requirement 6.2).
 *   * On a failed poll, keep the last successful snapshot on screen
 *     and let the next tick retry (Requirement 6.3).
 *   * Clear the polling interval on unmount so navigating away
 *     stops the network traffic.
 *
 * Safe rendering: every string that originates from the server is
 * interpolated through React's default text rendering; no
 * ``dangerouslySetInnerHTML`` anywhere (Requirements 13.1, 13.2).
 * The ESLint rule from task 12.8 (``react/no-danger: error``) will
 * catch any regression.
 *
 * Top-N: the backend decides how many entries to return from
 * ``GET /leaderboard``; the client simply renders everything it
 * receives. Lounge scale keeps the list short enough that no extra
 * client-side truncation is needed.
 */

import { useEffect, useRef, useState } from "react";

import {
  ApiRequestError,
  getLeaderboard,
  type LeaderboardEntry,
  type LeaderboardSnapshot,
} from "../api/client";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Poll cadence in milliseconds (Requirement 6.2).
 *
 * Exported so tests can assert the cadence against a named constant
 * rather than a magic number.
 */
export const LEADERBOARD_POLL_INTERVAL_MS = 1_000;

// ---------------------------------------------------------------------------
// Display formatting helpers
// ---------------------------------------------------------------------------

/**
 * Format ``wpm`` for display. WPM is a float; one decimal place is
 * enough precision for a lounge readout and matches the Results
 * page so the two surfaces feel consistent.
 */
export function formatWpm(wpm: number): string {
  if (!Number.isFinite(wpm)) return "0.0";
  return wpm.toFixed(1);
}

/**
 * Format ``accuracy`` as a percentage. The backend guarantees
 * accuracy is in ``[0, 100]`` (Requirement 4.2) so we simply round
 * to one decimal place and append ``%``.
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

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; snapshot: LeaderboardSnapshot }
  | { kind: "error"; message: string };

export function DashboardPage(): JSX.Element {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  // Track the previous rank-1 playerId so we can detect when a new
  // player takes the top spot (Requirement 12.1).
  const prevTopPlayerRef = useRef<string | null>(null);

  // When a new top player is detected, this holds their playerId so
  // the LeaderboardTable can apply the crown-glow animation class.
  const [crownPlayerId, setCrownPlayerId] = useState<string | null>(null);

  // Track whether the component is still mounted so a late-arriving
  // ``fetch`` response from before unmount can't call ``setState``
  // on a torn-down component. ``useRef`` gives us a stable mutable
  // handle that doesn't trigger re-renders.
  const mountedRef = useRef(true);

  // Detect new top player when the snapshot changes (Requirement 12).
  useEffect(() => {
    if (state.kind !== "ready") return;
    const entries = state.snapshot.entries;
    if (entries.length === 0) return;

    const currentTopId = entries[0]!.playerId;

    // Only trigger the crown animation when the top player actually
    // changes AND this is not the initial load (ref starts as null).
    if (
      prevTopPlayerRef.current !== null &&
      prevTopPlayerRef.current !== currentTopId
    ) {
      setCrownPlayerId(currentTopId);
    }

    // Always update the ref to the current rank-1 player.
    prevTopPlayerRef.current = currentTopId;
  }, [state]);

  useEffect(() => {
    mountedRef.current = true;

    /**
     * Single poll: fetch the snapshot, and on success replace the
     * rendered state. On failure, *keep* the previous state — the
     * dashboard must not blank out on a transient network blip
     * (Requirement 6.3). The next ``setInterval`` tick will retry.
     */
    async function poll(): Promise<void> {
      try {
        const snapshot = await getLeaderboard();
        if (!mountedRef.current) return;
        setState({ kind: "ready", snapshot });
      } catch (err) {
        if (!mountedRef.current) return;
        setState((prev) => {
          // If we already have a successful snapshot on screen,
          // preserve it — the user sees stale but useful data while
          // the backend recovers (Requirement 6.3).
          if (prev.kind === "ready") return prev;
          // No snapshot yet — surface an initial-load error so the
          // user isn't staring at a silent loading indicator. The
          // error surface still clears the next time a poll
          // succeeds.
          const message =
            err instanceof ApiRequestError
              ? err.error.message
              : "Unable to reach the server.";
          return { kind: "error", message };
        });
      }
    }

    // Kick off the initial snapshot fetch immediately on mount
    // (task 13.1) before starting the polling cadence.
    void poll();

    // Poll every 1s while mounted (task 13.3 / Requirement 6.2).
    // ``setInterval`` is fine here: the leaderboard request is
    // idempotent and cheap, and missing a tick (e.g. while a tab
    // is backgrounded) just means the next snapshot lands one
    // interval later.
    const intervalId = window.setInterval(() => {
      void poll();
    }, LEADERBOARD_POLL_INTERVAL_MS);

    return () => {
      mountedRef.current = false;
      window.clearInterval(intervalId);
    };
  }, []);

  return (
    <main>
      <h1>Leaderboard</h1>

      {state.kind === "loading" ? (
        <p data-testid="dashboard-loading">Loading leaderboard…</p>
      ) : null}

      {state.kind === "error" ? (
        <p role="alert" data-testid="dashboard-error">
          {state.message}
        </p>
      ) : null}

      {state.kind === "ready" ? (
        <LeaderboardTable
          entries={state.snapshot.entries}
          crownPlayerId={crownPlayerId}
          onCrownAnimationEnd={() => setCrownPlayerId(null)}
        />
      ) : null}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Leaderboard table
// ---------------------------------------------------------------------------

/**
 * Render the top-N rows of the leaderboard. Columns follow
 * Requirement 6.1 verbatim: ``nickname``, ``bestWpm``,
 * ``bestAccuracy``, ``bestPoints``. ``rank`` is included as the
 * leading column because it's the natural way to read a leaderboard
 * but is not a required field per the requirement.
 *
 * The row ``key`` is the server-issued ``playerId`` which is stable
 * across polls. That keeps React reconciliation cheap and prevents
 * focus / scroll jumps as rows shuffle between snapshots.
 *
 * Empty snapshots render a friendly placeholder instead of an empty
 * table so the dashboard doesn't look broken on a fresh lounge.
 */
function LeaderboardTable({
  entries,
  crownPlayerId,
  onCrownAnimationEnd,
}: {
  entries: readonly LeaderboardEntry[];
  crownPlayerId: string | null;
  onCrownAnimationEnd: () => void;
}): JSX.Element {
  if (entries.length === 0) {
    return (
      <p data-testid="dashboard-empty">
        No scores yet. Be the first to play!
      </p>
    );
  }

  return (
    <table data-testid="dashboard-table">
      <thead>
        <tr>
          <th scope="col">Rank</th>
          <th scope="col">Nickname</th>
          <th scope="col">WPM</th>
          <th scope="col">Accuracy</th>
          <th scope="col">Points</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => {
          const isCrownRow = entry.playerId === crownPlayerId;
          return (
            <tr
              key={entry.playerId}
              data-testid={`dashboard-row-${entry.playerId}`}
              className={isCrownRow ? "crown-glow" : undefined}
              onAnimationEnd={isCrownRow ? onCrownAnimationEnd : undefined}
            >
              <td data-testid="dashboard-cell-rank">{entry.rank}</td>
              <td data-testid="dashboard-cell-nickname">{entry.nickname}</td>
              <td data-testid="dashboard-cell-wpm">
                {formatWpm(entry.bestWpm)}
              </td>
              <td data-testid="dashboard-cell-accuracy">
                {formatAccuracy(entry.bestAccuracy)}
              </td>
              <td data-testid="dashboard-cell-points">
                {formatPoints(entry.bestPoints)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
