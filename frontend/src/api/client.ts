/**
 * Thin ``fetch`` wrapper for the Typing Game backend (task 12.1).
 *
 * Responsibilities:
 *   * Attach ``Authorization: Bearer <sessionToken>`` when a token is
 *     stored in ``localStorage``.
 *   * Parse the shared :mod:`ApiError` envelope on non-2xx responses
 *     and surface it as an :class:`ApiRequestError` so callers can
 *     branch on ``code`` / ``status``.
 *   * On 401 (``session_expired``), clear local state and redirect to
 *     ``/`` so the player re-registers (Requirement 7.4).
 *
 * The module intentionally avoids any dependency on react-router:
 * redirects use ``window.location.assign`` so the helper can be called
 * from anywhere (pages, polling timers, non-component code). Tests
 * stub ``fetch`` and ``window.location`` directly.
 *
 * Requirements addressed:
 *   * 7.4 — 401 clears local state and redirects to Nickname page.
 */

import { isApiError, type ApiError } from "./types";

// ---------------------------------------------------------------------------
// LocalStorage keys (kept here so pages don't hard-code strings).
// ---------------------------------------------------------------------------

export const SESSION_TOKEN_KEY = "typing-game.sessionToken";
export const PLAYER_ID_KEY = "typing-game.playerId";
export const NICKNAME_KEY = "typing-game.nickname";

// ---------------------------------------------------------------------------
// Base URL
// ---------------------------------------------------------------------------

/**
 * Base URL for the backend API. Resolved from ``VITE_API_BASE_URL`` at
 * build time (or runtime via Vite's ``import.meta.env``), falling back
 * to a same-origin empty prefix so dev and preview work without extra
 * config.
 */
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(
    /\/+$/,
    "",
  ) ?? "";

// ---------------------------------------------------------------------------
// Session helpers
// ---------------------------------------------------------------------------

/** Read the stored session token, or ``null`` if unset / unavailable. */
export function getSessionToken(): string | null {
  try {
    return window.localStorage.getItem(SESSION_TOKEN_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist a newly issued session after a successful ``POST /players``.
 * All three fields are co-written so the client has consistent local
 * state (or none).
 */
export function setSession(params: {
  sessionToken: string;
  playerId: string;
  nickname: string;
}): void {
  try {
    window.localStorage.setItem(SESSION_TOKEN_KEY, params.sessionToken);
    window.localStorage.setItem(PLAYER_ID_KEY, params.playerId);
    window.localStorage.setItem(NICKNAME_KEY, params.nickname);
  } catch {
    // localStorage can throw in private-browsing modes; the client
    // will simply re-authenticate on the next request.
  }
}

/** Remove any persisted session data. Called on 401 and on explicit sign-out. */
export function clearSession(): void {
  try {
    window.localStorage.removeItem(SESSION_TOKEN_KEY);
    window.localStorage.removeItem(PLAYER_ID_KEY);
    window.localStorage.removeItem(NICKNAME_KEY);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Error thrown by :func:`apiFetch` on any non-2xx response. Callers
 * can branch on ``status`` or the machine-readable ``error.code``.
 */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly error: ApiError;

  constructor(status: number, error: ApiError) {
    super(error.message);
    this.name = "ApiRequestError";
    this.status = status;
    this.error = error;
  }
}

// ---------------------------------------------------------------------------
// Core fetch helper
// ---------------------------------------------------------------------------

export interface ApiFetchOptions extends Omit<RequestInit, "body"> {
  /** Optional JSON body; serialized automatically. */
  json?: unknown;
  /** Skip attaching the session token (used by ``POST /players``). */
  skipAuth?: boolean;
}

/**
 * Issue a request against the backend and parse the response.
 *
 * On success, the decoded JSON body is returned with the caller's
 * requested shape (TypeScript enforces it via the generic).
 *
 * On failure:
 *   * If the response body is a well-formed :class:`ApiError`, an
 *     :class:`ApiRequestError` carrying the envelope is thrown.
 *   * Otherwise a generic :class:`ApiRequestError` with an
 *     ``internal_error`` code is thrown.
 *   * On HTTP 401, :func:`clearSession` is called and the browser is
 *     redirected to ``/`` *before* the error is thrown, matching
 *     Requirement 7.4.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const { json, skipAuth, headers, ...rest } = options;

  const finalHeaders = new Headers(headers);
  if (json !== undefined && !finalHeaders.has("Content-Type")) {
    finalHeaders.set("Content-Type", "application/json");
  }
  if (!skipAuth) {
    const token = getSessionToken();
    if (token && !finalHeaders.has("Authorization")) {
      finalHeaders.set("Authorization", `Bearer ${token}`);
    }
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: finalHeaders,
    body: json === undefined ? (rest as RequestInit).body : JSON.stringify(json),
  });

  if (response.status === 204) {
    return undefined as T;
  }

  // Best-effort JSON parse; guard against empty bodies.
  let body: unknown = null;
  const text = await response.text();
  if (text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      clearSession();
      // Only redirect if we're not already on the nickname page; tests
      // that stub ``window.location`` still observe the assignment.
      try {
        if (window.location.pathname !== "/") {
          window.location.assign("/");
        }
      } catch {
        // ignore (e.g. non-browser environments)
      }
    }
    const envelope: ApiError = isApiError(body)
      ? body
      : {
          code: "internal_error",
          message: `Request failed with status ${response.status}`,
        };
    throw new ApiRequestError(response.status, envelope);
  }

  return body as T;
}

// ---------------------------------------------------------------------------
// Typed endpoint helpers
// ---------------------------------------------------------------------------

/** Successful ``POST /players`` response (mirrors ``RegisterPlayerResponse``). */
export interface RegisterPlayerResponse {
  playerId: string;
  sessionToken: string;
  nickname: string;
  sessionExpiresAt: string;
}

/**
 * Register a nickname.
 *
 * Throws :class:`ApiRequestError` on 400 (``validation_error``) or
 * 409 (``nickname_taken``); callers render the ``error.message``
 * inline (Requirement 1.8).
 */
export async function registerPlayer(
  nickname: string,
): Promise<RegisterPlayerResponse> {
  return apiFetch<RegisterPlayerResponse>("/players", {
    method: "POST",
    json: { nickname },
    skipAuth: true,
  });
}

/**
 * Successful ``POST /games`` response (mirrors ``CreateGameResponse``).
 *
 * Field names track the backend wire format (camelCase). ``startAt``
 * is an ISO-8601 string; callers that need a ``Date`` can parse it
 * at the call site.
 */
export interface StartGameResponse {
  gameId: string;
  promptId: string;
  prompt: string;
  language: string;
  status: string;
  startAt: string;
}

/**
 * Start a new game for the authenticated player.
 *
 * Backend contract (task 8.2):
 *   * 201 → :class:`StartGameResponse`.
 *   * 409 ``game_conflict`` → body carries
 *     ``details.existingGameId`` so the client can offer the player
 *     a resume path (Requirement 2.6 / 2.7).
 *   * 401 → handled by :func:`apiFetch` (session cleared + redirect
 *     to ``/``).
 *
 * Throws :class:`ApiRequestError` for every non-2xx response.
 */
export async function startGame(): Promise<StartGameResponse> {
  return apiFetch<StartGameResponse>("/games", {
    method: "POST",
  });
}

/**
 * Successful ``POST /games/{gameId}/begin`` response (mirrors
 * ``BeginGameResponse``).
 *
 * Returns the authoritative ``startedAt`` timestamp (ISO-8601) the
 * server recorded when transitioning the Game to ``in_progress``.
 * The Countdown + Typing page uses this to align its elapsed-time
 * measurement, although the server ultimately computes elapsed from
 * ``endedAt - startedAt`` for scoring (Requirement 15.1 / 15.2).
 */
export interface BeginGameResponse {
  gameId: string;
  status: string;
  startedAt: string;
  promptId: string;
  prompt: string;
}

/**
 * Mark the typing phase as started for ``gameId``.
 *
 * Backend contract (task 8.3):
 *   * 200 → :class:`BeginGameResponse`.
 *   * 404 → Game does not exist or is not owned by the caller.
 *   * 409 ``game_conflict`` → Game is not in ``pending`` status
 *     (e.g. already in-progress or already completed).
 *   * 401 → handled by :func:`apiFetch`.
 *
 * Throws :class:`ApiRequestError` for every non-2xx response.
 */
export async function beginGame(gameId: string): Promise<BeginGameResponse> {
  return apiFetch<BeginGameResponse>(
    `/games/${encodeURIComponent(gameId)}/begin`,
    { method: "POST" },
  );
}

/**
 * Successful ``POST /games/{gameId}/result`` response (mirrors
 * ``SubmitResultResponse``).
 */
export interface SubmitResultResponse {
  gameId: string;
  wpm: number;
  accuracy: number;
  points: number;
  rank: number;
  endedAt: string;
}

/**
 * Submit the final typed text for ``gameId``.
 *
 * ``elapsedMs`` is the client-observed elapsed time in milliseconds.
 * The server ignores it for scoring per Requirement 3.6 / 15.2, but
 * the endpoint accepts the field for backward compatibility and for
 * server-side analytics; we therefore send it as ``elapsedSeconds``
 * (converted) so the wire shape matches
 * :class:`SubmitResultRequest`.
 *
 * Backend contract (task 8.4):
 *   * 200 → :class:`SubmitResultResponse`.
 *   * 409 ``game_timeout`` → the Game timed out server-side; the
 *     client surfaces a "time's up" message (Requirement 9.3).
 *   * 409 ``game_conflict`` → the Game is not in ``in_progress`` (e.g.
 *     already completed or abandoned).
 *   * 404 → Game does not exist or is not owned by the caller.
 *   * 400 ``validation_error`` → typed text exceeds the allowed
 *     length for the prompt.
 *   * 401 → handled by :func:`apiFetch`.
 *
 * Throws :class:`ApiRequestError` for every non-2xx response.
 */
export async function submitResult(
  gameId: string,
  typedText: string,
  elapsedMs: number,
): Promise<SubmitResultResponse> {
  return apiFetch<SubmitResultResponse>(
    `/games/${encodeURIComponent(gameId)}/result`,
    {
      method: "POST",
      json: {
        typedText,
        // Wire format is seconds (float). Convert from ms here so
        // callers work in the more natural ``performance.now()``
        // delta unit.
        elapsedSeconds: elapsedMs / 1000,
      },
    },
  );
}

/**
 * Successful ``GET /games/{gameId}`` response (mirrors
 * ``GameMetadataResponse``).
 *
 * Used by the Countdown + Typing page as a fallback when the
 * sessionStorage hand-off from the Ready page is missing (e.g. the
 * player refreshed the tab mid-flow).
 */
export interface GameMetadataResponse {
  gameId: string;
  playerId: string;
  promptId: string;
  prompt: string;
  language: string;
  status: string;
  startedAt: string | null;
  endedAt: string | null;
}

/**
 * Fetch metadata for a Game.
 *
 * Backend contract (task 8.6):
 *   * 200 → :class:`GameMetadataResponse`.
 *   * 404 → unknown ``gameId``.
 *
 * The endpoint is unauthenticated per the design; we still let
 * :func:`apiFetch` attach a token if one is stored.
 */
export async function getGame(
  gameId: string,
): Promise<GameMetadataResponse> {
  return apiFetch<GameMetadataResponse>(
    `/games/${encodeURIComponent(gameId)}`,
    { method: "GET" },
  );
}

/**
 * One row of the ``GET /leaderboard`` response (mirrors
 * ``LeaderboardEntryResponse`` on the backend).
 *
 * Field names track the backend wire format (camelCase) and the
 * Model 5 contract from the design doc:
 *   * ``playerId`` — stable identifier for the player
 *   * ``nickname`` — display name
 *   * ``bestWpm`` — player's best WPM across all Scores
 *   * ``bestAccuracy`` — player's best accuracy across all Scores
 *   * ``bestPoints`` — player's best composite points
 *   * ``rank`` — 1-based rank within the snapshot
 */
export interface LeaderboardEntry {
  playerId: string;
  nickname: string;
  bestWpm: number;
  bestAccuracy: number;
  bestPoints: number;
  rank: number;
}

/**
 * Full ``GET /leaderboard`` response (mirrors
 * ``LeaderboardResponse``).
 *
 * ``generatedAt`` is an ISO-8601 timestamp the backend sets when it
 * built the snapshot; the Dashboard_Client can use it to render a
 * "last updated" hint if desired.
 */
export interface LeaderboardSnapshot {
  entries: LeaderboardEntry[];
  generatedAt: string;
}

/**
 * Fetch the current leaderboard snapshot.
 *
 * Backend contract (task 8.5 / 10.1):
 *   * 200 → :class:`LeaderboardSnapshot`.
 *   * The endpoint is unauthenticated and recomputes the snapshot
 *     from the Scores table on every call — safe for 1 Hz polling
 *     from the Dashboard_Client (Requirements 6.1, 6.2).
 *
 * :func:`apiFetch` will still attach ``Authorization`` if a token
 * is stored; that's harmless (the backend ignores it for this
 * endpoint) and avoids a special-case ``skipAuth`` branch.
 *
 * Throws :class:`ApiRequestError` for any non-2xx response.
 */
export async function getLeaderboard(): Promise<LeaderboardSnapshot> {
  return apiFetch<LeaderboardSnapshot>("/leaderboard", { method: "GET" });
}
