/**
 * Nickname entry page (route ``/``) — task 12.2.
 *
 * Responsibilities:
 *   * Render the nickname input form (Requirement 1.1).
 *   * Validate the submitted nickname on the client with the same
 *     rules enforced by the server (Requirements 1.5, 1.6):
 *       - Length in ``[2, 20]`` after trimming.
 *       - Characters limited to ``[A-Za-z0-9 _-]``.
 *   * Submit to ``POST /players`` on success (Requirement 1.2).
 *   * On 201, persist the session and navigate to ``/ready``
 *     (Requirement 1.4).
 *   * On 400 / 409, render the server's message inline and keep the
 *     player on this page (Requirement 1.8).
 *
 * The page uses React's default text interpolation everywhere it
 * renders user-supplied or server-supplied strings; no
 * ``dangerouslySetInnerHTML``.
 */

import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiRequestError,
  registerPlayer,
  setSession,
} from "../api/client";

// ---------------------------------------------------------------------------
// Client-side validation (mirrors backend :mod:`app.domain.nickname`).
// ---------------------------------------------------------------------------

/** Minimum allowed nickname length, inclusive (Requirement 1.5). */
export const NICKNAME_MIN_LENGTH = 2;

/** Maximum allowed nickname length, inclusive (Requirement 1.5). */
export const NICKNAME_MAX_LENGTH = 20;

/** Allowed characters (Requirement 1.6). */
export const NICKNAME_ALLOWED_PATTERN = /^[A-Za-z0-9 _-]*$/;

export type NicknameValidationError =
  | { kind: "length"; length: number }
  | { kind: "charset"; invalidChars: string[] };

/**
 * Validate a nickname against the same rules the server enforces.
 * Returns ``null`` when the nickname is acceptable.
 *
 * The caller is responsible for trimming; this matches the server
 * which does *not* strip whitespace automatically.
 */
export function validateNickname(
  value: string,
): NicknameValidationError | null {
  if (
    value.length < NICKNAME_MIN_LENGTH ||
    value.length > NICKNAME_MAX_LENGTH
  ) {
    return { kind: "length", length: value.length };
  }
  if (!NICKNAME_ALLOWED_PATTERN.test(value)) {
    const seen = new Map<string, true>();
    for (const ch of value) {
      if (!/[A-Za-z0-9 _-]/.test(ch) && !seen.has(ch)) {
        seen.set(ch, true);
      }
    }
    return { kind: "charset", invalidChars: Array.from(seen.keys()) };
  }
  return null;
}

function messageForValidationError(err: NicknameValidationError): string {
  if (err.kind === "length") {
    return `Nickname must be between ${NICKNAME_MIN_LENGTH} and ${NICKNAME_MAX_LENGTH} characters.`;
  }
  return "Nickname may only contain letters, digits, spaces, hyphens, and underscores.";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function NicknamePage(): JSX.Element {
  const navigate = useNavigate();

  const [nickname, setNickname] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<boolean>(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting) return;

    setError(null);

    // Trim before validating so a run of trailing spaces doesn't slip
    // through the length check; the backend matches on the exact
    // submitted string so we send the trimmed value too.
    const candidate = nickname.trim();

    const localError = validateNickname(candidate);
    if (localError !== null) {
      setError(messageForValidationError(localError));
      return;
    }

    setSubmitting(true);
    try {
      const result = await registerPlayer(candidate);
      setSession({
        sessionToken: result.sessionToken,
        playerId: result.playerId,
        nickname: result.nickname,
      });
      navigate("/ready");
    } catch (err) {
      if (err instanceof ApiRequestError) {
        // Requirement 1.8: render the server-supplied message inline
        // and keep the player on the Nickname page. The backend uses
        // stable codes so we could branch on ``err.error.code``; the
        // default behavior is the same regardless, so we just show
        // the message.
        setError(err.error.message);
      } else {
        setError(
          "Unable to reach the server. Check your connection and try again.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main>
      <h1>Typing Game</h1>
      <p>Enter your nickname to begin.</p>
      <form onSubmit={handleSubmit} noValidate>
        <label htmlFor="nickname-input">Nickname</label>
        <input
          id="nickname-input"
          name="nickname"
          type="text"
          autoComplete="off"
          autoFocus
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          maxLength={NICKNAME_MAX_LENGTH}
          minLength={NICKNAME_MIN_LENGTH}
          aria-invalid={error !== null}
          aria-describedby={error !== null ? "nickname-error" : undefined}
          disabled={submitting}
          data-testid="nickname-input"
        />
        <button
          type="submit"
          disabled={submitting || nickname.trim().length === 0}
          data-testid="nickname-submit"
        >
          {submitting ? "Joining…" : "Start"}
        </button>
        {error !== null ? (
          <p
            id="nickname-error"
            role="alert"
            data-testid="nickname-error"
          >
            {error}
          </p>
        ) : null}
      </form>
    </main>
  );
}
