/**
 * Shared API error-response contract mirrored from the backend
 * (`backend/app/errors.py :: ApiError`).
 *
 * Keep this file in sync with the backend whenever the error envelope
 * or the list of error codes changes.
 *
 * Covers Requirements 1.7, 1.8, 2.6, 7.3, 9.2, 12.2, 14.3.
 */

/**
 * Stable, machine-readable error codes returned by the API.
 * HTTP status mapping (per design):
 *   400 validation_error
 *   401 session_expired
 *   404 not_found
 *   409 nickname_taken | game_conflict | game_timeout
 *   429 rate_limited
 */
export type ApiErrorCode =
  | "validation_error"
  | "nickname_taken"
  | "session_expired"
  | "not_found"
  | "game_conflict"
  | "game_timeout"
  | "rate_limited"
  | "internal_error";

/**
 * Envelope returned for every non-2xx API response.
 *
 * `details` is an optional bag for structured context, e.g.:
 *  - `{ gameId: "..." }` on a 409 `game_conflict`
 *  - `{ errors: [...] }` on a 400 `validation_error`
 */
export interface ApiError {
  code: ApiErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

/**
 * Narrowing helper for distinguishing backend error envelopes from arbitrary
 * JSON payloads (e.g. inside a generic `fetch` response handler).
 */
export function isApiError(value: unknown): value is ApiError {
  if (typeof value !== "object" || value === null) return false;
  const maybe = value as Record<string, unknown>;
  return typeof maybe.code === "string" && typeof maybe.message === "string";
}
