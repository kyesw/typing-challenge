/**
 * Component tests for the Countdown + Typing page (task 12.4).
 *
 * Covers:
 *   * Countdown ticks 3 → 2 → 1 one second per tick under fake
 *     timers (Requirement 3.1).
 *   * After the countdown, ``POST /games/{id}/begin`` fires and the
 *     prompt + typing input are revealed (Requirement 3.2).
 *   * Typing produces per-character feedback classes (correct /
 *     incorrect / pending) without any server round-trip per
 *     keystroke (Requirements 3.3, 3.4).
 *   * Completing the prompt submits ``POST /games/{id}/result`` and
 *     navigates to ``/results/:gameId`` with a persisted result
 *     hand-off (Requirement 3.5).
 *   * When the sessionStorage hand-off from /ready is absent, the
 *     page falls back to ``GET /games/{gameId}``.
 *   * Server-side timeout (409 ``game_timeout``) surfaces the
 *     timeout UI instead of navigating (Requirement 9.3 foreshadow).
 *   * :func:`classifyView` purity test.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  classifyView,
  isAbandonedConflict,
  PlayPage,
  RESULT_HANDOFF_KEY_PREFIX,
  RETRY_BACKOFF_MS,
  TIMEOUT_REDIRECT_MS,
  readResultHandoff,
} from "./PlayPage";
import {
  persistGameHandoff,
} from "./ReadyPage";
import { ApiRequestError, SESSION_TOKEN_KEY } from "../api/client";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

const PROMPT = "ab cd"; // short prompt so completion tests are fast
const GAME_ID = "g1";

function renderPlay(initialGameId = GAME_ID) {
  return render(
    <MemoryRouter initialEntries={[`/play/${initialGameId}`]}>
      <Routes>
        <Route path="/play/:gameId" element={<PlayPage />} />
        <Route
          path="/results/:gameId"
          element={<ResultsMarker />}
        />
        <Route
          path="/ready"
          element={<div data-testid="ready-marker">ready</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function ResultsMarker() {
  return <div data-testid="results-marker">results</div>;
}

type FetchResponseSpec = {
  status: number;
  body?: unknown;
  /** Optional predicate to assert this is the right URL. */
  urlMatch?: RegExp;
  /**
   * If true, the stub rejects the promise with a ``TypeError`` —
   * simulating a network-layer failure (``fetch`` rejects when
   * the browser cannot reach the server, which is what the retry
   * path triggers on for task 12.7).
   */
  networkError?: boolean;
};

/**
 * Stub ``window.fetch`` to return the given specs in order, one per
 * call. If a spec has ``urlMatch`` it's checked against the actual
 * URL so we don't accidentally match the wrong request.
 */
function stubFetchSequence(
  specs: readonly FetchResponseSpec[],
): ReturnType<typeof vi.fn> {
  let idx = 0;
  const mock = vi.fn(
    (url: RequestInfo | URL, _init?: RequestInit) => {
      const spec = specs[Math.min(idx, specs.length - 1)]!;
      idx += 1;
      if (spec.urlMatch !== undefined) {
        expect(String(url)).toMatch(spec.urlMatch);
      }
      if (spec.networkError === true) {
        // ``fetch`` rejects with TypeError when the browser can't
        // reach the server; see
        // https://developer.mozilla.org/docs/Web/API/Window/fetch
        return Promise.reject(new TypeError("NetworkError"));
      }
      const text = spec.body !== undefined ? JSON.stringify(spec.body) : "";
      return Promise.resolve({
        ok: spec.status >= 200 && spec.status < 300,
        status: spec.status,
        text: () => Promise.resolve(text),
      } as unknown as Response);
    },
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

/**
 * Advance fake timers by 1000 ms inside an ``act`` wrapper so React
 * state transitions flush before the next assertion.
 */
async function tick(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

const beginBody = {
  gameId: GAME_ID,
  status: "in_progress",
  startedAt: "2030-01-01T00:00:00Z",
  promptId: "pr1",
  prompt: PROMPT,
};

const resultBody = {
  gameId: GAME_ID,
  wpm: 42.5,
  accuracy: 100,
  points: 425,
  rank: 3,
  endedAt: "2030-01-01T00:00:10Z",
};

const gameMetadataBody = {
  gameId: GAME_ID,
  playerId: "p1",
  promptId: "pr1",
  prompt: PROMPT,
  language: "en",
  status: "pending",
  startedAt: null,
  endedAt: null,
};

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("classifyView", () => {
  it("places the cursor at index 0 and leaves the rest pending when nothing is typed", () => {
    // Empty typed → index 0 is the Cursor_Position; everything else
    // is pending. No typo in a no-input snapshot.
    const result = classifyView("abc", "");
    expect(result.states).toEqual(["current", "pending", "pending"]);
    expect(result.cursorIndex).toBe(0);
  });

  it("marks correct characters correct and incorrect ones incorrect", () => {
    // Prompt "abc", typed "aXc": position 0 matches, position 1 is a
    // typo, position 2 matches. Cursor has moved past the end so no
    // span is ``current`` (typed.length === prompt.length).
    const result = classifyView("abc", "aXc");
    expect(result.states).toEqual(["correct", "incorrect", "correct"]);
    expect(result.cursorIndex).toBe(3);
  });

  it("does not cascade — a mismatch at position N leaves later correct characters correct", () => {
    const result = classifyView("abcd", "aXcd");
    expect(result.states).toEqual([
      "correct",
      "incorrect",
      "correct",
      "correct",
    ]);
    expect(result.cursorIndex).toBe(4);
  });

  it("marks the next position as current and everything past it as pending", () => {
    // Prompt "abcde", typed "ab": positions 0 and 1 are correct, the
    // Cursor_Position is index 2, and indices 3 and 4 are pending.
    const result = classifyView("abcde", "ab");
    expect(result.states).toEqual([
      "correct",
      "correct",
      "current",
      "pending",
      "pending",
    ]);
    expect(result.cursorIndex).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// PlayPage component
// ---------------------------------------------------------------------------

describe("PlayPage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("ticks the countdown 3 → 2 → 1 with fake timers and then calls POST /begin", async () => {
    vi.useFakeTimers();
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    const fetchMock = stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
    ]);

    renderPlay();

    // Immediately after mount the countdown shows 3.
    expect(screen.getByTestId("play-countdown")).toHaveTextContent("3");
    expect(fetchMock).not.toHaveBeenCalled();

    await tick(1000);
    expect(screen.getByTestId("play-countdown")).toHaveTextContent("2");
    expect(fetchMock).not.toHaveBeenCalled();

    await tick(1000);
    expect(screen.getByTestId("play-countdown")).toHaveTextContent("1");
    expect(fetchMock).not.toHaveBeenCalled();

    // Final tick schedules the transition to "beginning" which fires
    // POST /begin; we need to flush microtasks for the await to
    // resolve inside the effect. Using real timers from here makes the
    // async plumbing simpler.
    await tick(1000);
    vi.useRealTimers();
    await screen.findByTestId("play-input");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toMatch(/\/games\/g1\/begin$/);
    expect((init as RequestInit).method).toBe("POST");
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Authorization")).toBe("Bearer tok");

    // Prompt is rendered character-by-character. With nothing typed
    // yet, index 0 is the Cursor_Position (``current``) and the rest
    // are ``pending`` — the four-valued classification from the
    // typing-input-highlighting feature (Requirements 1.5, 1.6, 3.1).
    for (let i = 0; i < PROMPT.length; i += 1) {
      const cell = screen.getByTestId(`play-char-${i}`);
      expect(cell).toHaveAttribute(
        "data-char-class",
        i === 0 ? "current" : "pending",
      );
      // Using textContent directly (rather than toHaveTextContent,
      // which collapses whitespace) so the space character at
      // PROMPT[2] is exercised properly.
      expect(cell.textContent).toBe(PROMPT[i]!);
    }
  });

  it("falls back to GET /games/{id} when the sessionStorage hand-off is absent", async () => {
    const fetchMock = stubFetchSequence([
      { status: 200, body: gameMetadataBody, urlMatch: /\/games\/g1$/ },
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
    ]);

    renderPlay();

    // First call is GET /games/{id} to fetch the prompt.
    await screen.findByTestId("play-countdown");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const firstInit = fetchMock.mock.calls[0]![1] as RequestInit | undefined;
    expect((firstInit?.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("marks correct and incorrect keystrokes without extra server calls", async () => {
    const user = userEvent.setup();
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    const fetchMock = stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
    ]);

    renderPlay();

    // Wait past countdown and begin.
    const input = await screen.findByTestId("play-input", {}, { timeout: 5000 });

    // One fetch (POST /begin). Subsequent typing must not trigger more.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Prompt is "ab cd"; type "aX" — position 0 correct, position 1
    // incorrect, position 2 is the Cursor_Position (``current``), and
    // the remaining positions are pending. The ``current`` state is
    // part of the four-valued classification introduced by the
    // typing-input-highlighting feature (Requirement 1.6).
    await user.type(input, "aX");
    expect(screen.getByTestId("play-char-0")).toHaveAttribute(
      "data-char-class",
      "correct",
    );
    expect(screen.getByTestId("play-char-1")).toHaveAttribute(
      "data-char-class",
      "incorrect",
    );
    expect(screen.getByTestId("play-char-2")).toHaveAttribute(
      "data-char-class",
      "current",
    );
    expect(screen.getByTestId("play-char-3")).toHaveAttribute(
      "data-char-class",
      "pending",
    );

    // No additional fetches.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("caps pasted input at the prompt length and submits only the truncated text via POST /result (Requirements 5.1, 5.2)", async () => {
    // Pasting a string longer than the prompt into the typing input
    // must not let ``state.typed`` grow past ``prompt.length``. The
    // onChange handler slices the incoming value to the prompt
    // length (see ``raw.slice(0, current.prompt.length)`` in
    // PlayPage.tsx), which keeps the controlled input aligned with
    // the highlighted display.
    //
    // Why assert on the POST /result body rather than the rendered
    // DOM: a paste whose length exceeds the prompt drives the
    // capped ``typed`` to ``prompt.length`` in a single onChange,
    // which immediately satisfies the completion condition
    // (``typed.length === prompt.length``). PlayPage transitions to
    // the ``submitting`` state and fires POST /result in the same
    // render cycle, unmounting the typing surface. There is no
    // post-cap ``typing`` render to query for Character_Display
    // spans in this scenario, so the end-to-end guarantee of
    // Requirements 5.1 / 5.2 is observed via the request body the
    // server receives — which is exactly what the cap is supposed
    // to protect.
    //
    // The classifier→renderer projection for non-completing inputs
    // (i.e. when ``slice(0, prompt.length).length < prompt.length``)
    // is already covered by Property 2 in TypingView.pbt.test.tsx,
    // which asserts every rendered span's ``data-char-class``
    // equals ``classifyView(prompt, typed).states[i]`` for
    // arbitrary ``(prompt, typed)`` drawn from fast-check.
    //
    // We drive the change via ``fireEvent.change`` (instead of
    // ``userEvent.type`` or ``userEvent.paste``) because it bypasses
    // the HTML ``maxLength`` attribute — that attribute only blocks
    // keystrokes/pastes issued through user interaction, not direct
    // assignments to ``input.value``. This genuinely exercises the
    // onChange-level cap rather than relying on the browser to cap
    // for us.
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    const fetchMock = stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
      { status: 200, body: resultBody, urlMatch: /\/games\/g1\/result$/ },
    ]);

    renderPlay();

    const input = (await screen.findByTestId(
      "play-input",
      {},
      { timeout: 5000 },
    )) as HTMLInputElement;

    // Begin fired, no result yet.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Pasted string longer than the prompt. The leading characters
    // intentionally mix matching and mismatching glyphs so the
    // submitted typedText is non-trivial to eyeball (not
    // all-correct, not all-incorrect).
    const pasted = "aX cdEFGHIJ"; // PROMPT = "ab cd" (length 5)
    expect(pasted.length).toBeGreaterThan(PROMPT.length);

    await act(async () => {
      fireEvent.change(input, { target: { value: pasted } });
    });

    // Wait for POST /result to be dispatched. The cap runs inside
    // onChange and — because ``pasted.length > PROMPT.length``
    // implies the capped value's length equals PROMPT.length —
    // completion fires immediately, so a second fetch is queued.
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    // Extract the /result request body and assert the cap is
    // observed server-side.
    const submitCall = fetchMock.mock.calls[1]!;
    expect(String(submitCall[0])).toMatch(/\/games\/g1\/result$/);
    const submitInit = submitCall[1] as RequestInit;
    expect(submitInit.method).toBe("POST");
    const body = JSON.parse(submitInit.body as string);

    // The two core guarantees of Requirements 5.1 / 5.2: the
    // submitted typedText is exactly the prompt-length prefix of
    // the pasted value — never longer, never a different slice.
    expect(body.typedText.length).toBe(PROMPT.length);
    expect(body.typedText).toBe(pasted.slice(0, PROMPT.length));
  });

  it("submits POST /result on completion, persists the result, and navigates to /results/:gameId", async () => {
    const user = userEvent.setup();
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    const fetchMock = stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
      { status: 200, body: resultBody, urlMatch: /\/games\/g1\/result$/ },
    ]);

    renderPlay();

    const input = await screen.findByTestId("play-input", {}, { timeout: 5000 });

    // Type the whole prompt. userEvent.type() types one char at a
    // time, triggering the change handler for each; completion fires
    // automatically when typed.length === prompt.length.
    await user.type(input, PROMPT);

    // Navigation to /results/:gameId.
    expect(await screen.findByTestId("results-marker")).toBeInTheDocument();

    // Fetch #2 is POST /result with the typed text.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const submitCall = fetchMock.mock.calls[1]!;
    const submitInit = submitCall[1] as RequestInit;
    expect(String(submitCall[0])).toMatch(/\/games\/g1\/result$/);
    expect(submitInit.method).toBe("POST");
    const body = JSON.parse(submitInit.body as string);
    expect(body.typedText).toBe(PROMPT);
    expect(typeof body.elapsedSeconds).toBe("number");
    expect(body.elapsedSeconds).toBeGreaterThanOrEqual(0);

    // Result hand-off is persisted for the Results page.
    const stored = window.sessionStorage.getItem(
      `${RESULT_HANDOFF_KEY_PREFIX}${GAME_ID}`,
    );
    expect(stored).not.toBeNull();
    const handoff = readResultHandoff(GAME_ID);
    expect(handoff).toMatchObject({
      gameId: GAME_ID,
      wpm: 42.5,
      accuracy: 100,
      points: 425,
      rank: 3,
    });
  });

  it("shows the timeout UI when POST /result returns 409 game_timeout", async () => {
    const user = userEvent.setup();
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
      {
        status: 409,
        body: {
          code: "game_timeout",
          message: "Time's up — the game exceeded the maximum duration.",
        },
        urlMatch: /\/games\/g1\/result$/,
      },
    ]);

    renderPlay();

    const input = await screen.findByTestId("play-input", {}, { timeout: 5000 });
    await user.type(input, PROMPT);

    expect(await screen.findByTestId("play-timeout")).toBeInTheDocument();
    // No navigation yet — the auto-redirect has not fired.
    expect(screen.queryByTestId("results-marker")).toBeNull();
    expect(screen.queryByTestId("ready-marker")).toBeNull();

    // No result hand-off stored on timeout.
    expect(
      window.sessionStorage.getItem(`${RESULT_HANDOFF_KEY_PREFIX}${GAME_ID}`),
    ).toBeNull();
  });

  it("auto-navigates to /ready after the timeout redirect window (Requirement 9.3)", async () => {
    // Stay on fake timers end-to-end so the setTimeout the effect
    // schedules when entering the ``timeout`` state lands in the fake
    // scheduler. We drive the countdown tick-by-tick (same pattern
    // as the "ticks the countdown" test) and submit the typed text
    // via ``fireEvent.change`` instead of userEvent.type so we don't
    // have to interleave its pointer delays with the timer pump.
    vi.useFakeTimers();

    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
      {
        status: 409,
        body: {
          code: "game_timeout",
          message: "Time's up — the game exceeded the maximum duration.",
        },
        urlMatch: /\/games\/g1\/result$/,
      },
    ]);

    renderPlay();

    // Countdown: one second per tick, and each tick schedules the
    // next. advanceTimersByTimeAsync flushes both the timer and the
    // microtasks the effect's state update creates.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    // The "beginning" effect fires POST /begin. Flush the pending
    // promise callbacks by awaiting a zero-length advance.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const input = screen.getByTestId("play-input") as HTMLInputElement;
    // Submit the typed text in a single change; the onChange handler
    // transitions the page to ``submitting`` and the effect fires
    // POST /result, which we've stubbed to 409 game_timeout.
    await act(async () => {
      fireEvent.change(input, { target: { value: PROMPT } });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByTestId("play-timeout")).toBeInTheDocument();
    expect(screen.queryByTestId("ready-marker")).toBeNull();

    // Advance past the redirect window — the setTimeout scheduled by
    // the timeout effect drains here and navigate("/ready") runs.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TIMEOUT_REDIRECT_MS);
    });

    expect(screen.getByTestId("ready-marker")).toBeInTheDocument();
    // Timeout screen is gone because the route switched.
    expect(screen.queryByTestId("play-timeout")).toBeNull();
  });

  it("manual 'Back to Ready' button on the timeout screen navigates immediately", async () => {
    const user = userEvent.setup();
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    stubFetchSequence([
      { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
      {
        status: 409,
        body: {
          code: "game_timeout",
          message: "Time's up — the game exceeded the maximum duration.",
        },
        urlMatch: /\/games\/g1\/result$/,
      },
    ]);

    renderPlay();

    const input = await screen.findByTestId(
      "play-input",
      {},
      { timeout: 5000 },
    );
    await user.type(input, PROMPT);
    await screen.findByTestId("play-timeout");

    // Click the manual button immediately; no need to wait for the
    // auto-redirect.
    await user.click(screen.getByTestId("play-timeout-back"));
    expect(screen.getByTestId("ready-marker")).toBeInTheDocument();
  });

  it("surfaces a server error when POST /begin fails", async () => {
    persistGameHandoff({
      gameId: GAME_ID,
      promptId: "pr1",
      prompt: PROMPT,
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });
    stubFetchSequence([
      {
        status: 500,
        body: {
          code: "internal_error",
          message: "Server exploded.",
        },
        urlMatch: /\/games\/g1\/begin$/,
      },
    ]);

    renderPlay();

    expect(
      await screen.findByTestId("play-error", {}, { timeout: 5000 }),
    ).toHaveTextContent(/server exploded/i);
    // No prompt revealed.
    expect(screen.queryByTestId("play-input")).toBeNull();
  });

  it("round-trips the result hand-off through sessionStorage", () => {
    const key = `${RESULT_HANDOFF_KEY_PREFIX}abc`;
    expect(readResultHandoff("abc")).toBeNull();

    window.sessionStorage.setItem(
      key,
      JSON.stringify({
        gameId: "abc",
        wpm: 1,
        accuracy: 2,
        points: 3,
        rank: 4,
        endedAt: "2030-01-01T00:00:00Z",
        clientElapsedMs: 5000,
      }),
    );
    expect(readResultHandoff("abc")).toMatchObject({
      gameId: "abc",
      wpm: 1,
      rank: 4,
    });

    // Malformed JSON → null.
    window.sessionStorage.setItem(key, "{not-json");
    expect(readResultHandoff("abc")).toBeNull();
  });

  // ---------------------------------------------------------------------
  // Network-loss resilience during typing (task 12.7, Requirement 10.x)
  // ---------------------------------------------------------------------

  describe("network-loss resilience during result submission", () => {
    it("enters the retrying state when POST /result fails at the network layer", async () => {
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        // Further calls — e.g. a fallback backoff tick — keep failing
        // with the same network error so the page stays in
        // ``retrying`` for the duration of this test.
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);

      // After the network failure the page shows the retry UI and
      // does not navigate away or show a generic error.
      expect(
        await screen.findByTestId("play-retrying"),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("results-marker")).toBeNull();
      expect(screen.queryByTestId("play-error")).toBeNull();
      expect(screen.queryByTestId("play-timeout")).toBeNull();
      // No result hand-off persisted — the submission wasn't accepted.
      expect(
        window.sessionStorage.getItem(
          `${RESULT_HANDOFF_KEY_PREFIX}${GAME_ID}`,
        ),
      ).toBeNull();
    });

    it("retries on the browser's online event and navigates to /results on success (Requirement 10.3)", async () => {
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      const fetchMock = stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        { status: 200, body: resultBody, urlMatch: /\/games\/g1\/result$/ },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);
      await screen.findByTestId("play-retrying");

      // Fire ``online`` — the retry path should submit again
      // immediately without waiting for the backoff timer.
      await act(async () => {
        window.dispatchEvent(new Event("online"));
      });

      expect(
        await screen.findByTestId("results-marker", {}, { timeout: 5000 }),
      ).toBeInTheDocument();

      // Three calls total: /begin, the failed /result, the retry.
      expect(fetchMock).toHaveBeenCalledTimes(3);
      // The retry used the same typedText + elapsedSeconds the buffer
      // captured; elapsedSeconds is captured at completion, so it is
      // identical across the two /result calls.
      const firstResultBody = JSON.parse(
        (fetchMock.mock.calls[1]![1] as RequestInit).body as string,
      );
      const retryBody = JSON.parse(
        (fetchMock.mock.calls[2]![1] as RequestInit).body as string,
      );
      expect(retryBody.typedText).toBe(firstResultBody.typedText);
      expect(retryBody.typedText).toBe(PROMPT);
      expect(retryBody.elapsedSeconds).toBe(firstResultBody.elapsedSeconds);

      // Result hand-off persisted from the successful retry.
      const stored = readResultHandoff(GAME_ID);
      expect(stored).toMatchObject({
        gameId: GAME_ID,
        wpm: 42.5,
        rank: 3,
      });
    });

    it("retries on the fallback backoff timer when no online event fires", async () => {
      vi.useFakeTimers();

      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      const fetchMock = stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        { status: 200, body: resultBody, urlMatch: /\/games\/g1\/result$/ },
      ]);

      renderPlay();

      // Drive countdown + begin.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      const input = screen.getByTestId("play-input") as HTMLInputElement;
      await act(async () => {
        fireEvent.change(input, { target: { value: PROMPT } });
      });
      // Let the failed POST /result run.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(screen.getByTestId("play-retrying")).toBeInTheDocument();

      // Advance past the first backoff slot so the fallback timer
      // retries without needing an ``online`` event.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RETRY_BACKOFF_MS[0]!);
      });
      // Flush pending microtasks so the retry's success handler can
      // call ``navigate``.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(screen.getByTestId("results-marker")).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("shows the abandoned UI with a New game button when the retry returns 409 game_conflict + currentStatus=abandoned (Requirement 10.4)", async () => {
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        {
          status: 409,
          body: {
            code: "game_conflict",
            message: "Game cannot be completed from its current status.",
            details: {
              gameId: GAME_ID,
              currentStatus: "abandoned",
            },
          },
          urlMatch: /\/games\/g1\/result$/,
        },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);
      await screen.findByTestId("play-retrying");

      await act(async () => {
        window.dispatchEvent(new Event("online"));
      });

      expect(
        await screen.findByTestId("play-abandoned", {}, { timeout: 5000 }),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("results-marker")).toBeNull();
      // No result hand-off persisted.
      expect(
        window.sessionStorage.getItem(
          `${RESULT_HANDOFF_KEY_PREFIX}${GAME_ID}`,
        ),
      ).toBeNull();

      // "New game" navigates to /ready.
      await user.click(screen.getByTestId("play-abandoned-new-game"));
      expect(screen.getByTestId("ready-marker")).toBeInTheDocument();
    });

    it("treats a 409 game_timeout on retry as the abandoned outcome", async () => {
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        {
          status: 409,
          body: {
            code: "game_timeout",
            message: "Time's up — the game exceeded the maximum duration.",
          },
          urlMatch: /\/games\/g1\/result$/,
        },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);
      await screen.findByTestId("play-retrying");

      await act(async () => {
        window.dispatchEvent(new Event("online"));
      });

      // The retry path collapses game_timeout to the same abandoned
      // UI (Requirement 10.4): once the server has rejected the
      // buffered submission, the player's only next step is a new
      // game — the timeout-redirect countdown is for the happy path
      // where the failure was seen immediately and the player is
      // still "on the field".
      expect(
        await screen.findByTestId("play-abandoned", {}, { timeout: 5000 }),
      ).toBeInTheDocument();
    });

    it("recognizes an already-abandoned game on the first submission (no retry)", async () => {
      // Covers the edge case where the sweeper transitioned the game
      // to abandoned before the player's submission landed, but the
      // network itself was fine — so the failure arrives via an
      // ApiRequestError, not a network reject. We still want the
      // abandoned UI, not a generic "something went wrong" screen.
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        {
          status: 409,
          body: {
            code: "game_conflict",
            message: "Game cannot be completed from its current status.",
            details: {
              gameId: GAME_ID,
              currentStatus: "abandoned",
            },
          },
          urlMatch: /\/games\/g1\/result$/,
        },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);

      expect(
        await screen.findByTestId("play-abandoned", {}, { timeout: 5000 }),
      ).toBeInTheDocument();
    });

    it("buffers the same typedText + elapsedSeconds across a network failure (Requirement 10.1)", async () => {
      // Requirement 10.1: the local typing timer keeps running on
      // disconnect. In this implementation ``performance.now()`` is
      // the timer source and ``elapsedMs`` is captured at
      // completion — the retry path does not reset or recompute it.
      // Asserting equality between the first submission's
      // ``elapsedSeconds`` and the retry's verifies the buffered
      // measurement is preserved verbatim (the essence of "continue
      // the local timer" from the frontend's perspective).
      const user = userEvent.setup();
      persistGameHandoff({
        gameId: GAME_ID,
        promptId: "pr1",
        prompt: PROMPT,
        language: "en",
        status: "pending",
        startAt: "2030-01-01T00:00:00Z",
      });
      const fetchMock = stubFetchSequence([
        { status: 200, body: beginBody, urlMatch: /\/games\/g1\/begin$/ },
        { status: 0, networkError: true, urlMatch: /\/games\/g1\/result$/ },
        { status: 200, body: resultBody, urlMatch: /\/games\/g1\/result$/ },
      ]);

      renderPlay();
      const input = await screen.findByTestId(
        "play-input",
        {},
        { timeout: 5000 },
      );
      await user.type(input, PROMPT);
      await screen.findByTestId("play-retrying");

      await act(async () => {
        window.dispatchEvent(new Event("online"));
      });
      await screen.findByTestId("results-marker");

      const firstSubmit = JSON.parse(
        (fetchMock.mock.calls[1]![1] as RequestInit).body as string,
      );
      const retrySubmit = JSON.parse(
        (fetchMock.mock.calls[2]![1] as RequestInit).body as string,
      );
      expect(retrySubmit.elapsedSeconds).toBe(firstSubmit.elapsedSeconds);
      expect(retrySubmit.typedText).toBe(PROMPT);
      expect(firstSubmit.typedText).toBe(PROMPT);
    });
  });
});

// ---------------------------------------------------------------------------
// isAbandonedConflict unit tests
// ---------------------------------------------------------------------------

describe("isAbandonedConflict", () => {
  it("returns true for 409 game_conflict with currentStatus=abandoned", () => {
    const err = new ApiRequestError(409, {
      code: "game_conflict",
      message: "Game cannot be completed from its current status.",
      details: { gameId: "g1", currentStatus: "abandoned" },
    });
    expect(isAbandonedConflict(err)).toBe(true);
  });

  it("returns false for game_conflict with a different currentStatus", () => {
    const err = new ApiRequestError(409, {
      code: "game_conflict",
      message: "Game cannot be completed from its current status.",
      details: { gameId: "g1", currentStatus: "completed" },
    });
    expect(isAbandonedConflict(err)).toBe(false);
  });

  it("returns false for game_conflict without details", () => {
    const err = new ApiRequestError(409, {
      code: "game_conflict",
      message: "Game cannot be completed.",
    });
    expect(isAbandonedConflict(err)).toBe(false);
  });

  it("returns false for non-conflict codes", () => {
    const err = new ApiRequestError(409, {
      code: "game_timeout",
      message: "Time's up.",
    });
    expect(isAbandonedConflict(err)).toBe(false);
  });
});
