/**
 * Property-based test for the ``PlayPage`` local-only typing contract
 * (typing-input-highlighting design, Property 7).
 *
 * Feature: typing-input-highlighting, Property 7: No network request is issued during typing
 *
 * **Property 7: No network request is issued during typing**
 * **Validates: Requirements 6.1, 6.2**
 *
 *   *For any* prompt ``p`` and any sequence of user-typed inputs
 *   ``u_1, u_2, …, u_k`` that do not complete the prompt
 *   (``len(u_j) < len(p)`` for every ``j``), after mounting
 *   ``PlayPage`` with the sessionStorage hand-off path (so the only
 *   pre-typing request is ``POST /games/{gameId}/begin``) and firing
 *   the change events for each ``u_j``, the ``fetch`` mock SHALL
 *   have been called exactly once (for ``POST /begin``) across the
 *   entire sequence.
 *
 * Setup notes:
 *   * Helpers mirror the ones in ``PlayPage.test.tsx`` — duplicated
 *     (not imported) so the suite remains self-contained, which is
 *     the pattern the tasks plan explicitly permits. The other file
 *     is the canonical example test suite; property tests should
 *     not reach across into each other's fixtures.
 *   * ``vi.useFakeTimers()`` deterministically drains the 3 → 2 → 1
 *     countdown (Requirement 3.1 of the base Typing_Game spec).
 *     After the final tick the countdown transitions to the
 *     ``beginning`` state, whose effect fires ``POST /begin``; we
 *     flush the fetch promise by awaiting a zero-length advance
 *     (same pattern as the "auto-navigates to /ready" test in
 *     ``PlayPage.test.tsx``).
 *   * Each fast-check iteration clears ``sessionStorage`` /
 *     ``localStorage``, re-seeds the hand-off, re-stubs ``fetch``,
 *     and unmounts the previous ``PlayPage`` so iterations do not
 *     leak state into one another.
 *
 * Iterations: every ``PlayPage`` mount drives three countdown ticks
 * plus an async ``POST /begin`` flush, so iterations are
 * measurably more expensive than the direct ``TypingView`` property
 * tests. 100 iterations of the Property 7 property run in a few
 * seconds on CI, which is fine — we do not lower ``numRuns`` from
 * fast-check's default.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import fc from "fast-check";

import { PlayPage } from "./PlayPage";
import { persistGameHandoff } from "./ReadyPage";
import { SESSION_TOKEN_KEY } from "../api/client";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PROMPT = "ab cd"; // length 5 — matches ``PlayPage.test.tsx``
const GAME_ID = "g1";

const beginBody = {
  gameId: GAME_ID,
  status: "in_progress",
  startedAt: "2030-01-01T00:00:00Z",
  promptId: "pr1",
  prompt: PROMPT,
};

// ---------------------------------------------------------------------------
// Test helpers (duplicated from ``PlayPage.test.tsx``)
// ---------------------------------------------------------------------------

type FetchResponseSpec = {
  status: number;
  body?: unknown;
  urlMatch?: RegExp;
};

/**
 * Stub ``window.fetch`` to return the given specs in order, one per
 * call. If a spec has ``urlMatch`` it's checked against the actual
 * URL so we don't accidentally match the wrong request. This is a
 * stripped-down copy of the helper in ``PlayPage.test.tsx`` — the
 * Property 7 suite only needs the success path for ``POST /begin``.
 */
function stubFetchSequence(
  specs: readonly FetchResponseSpec[],
): ReturnType<typeof vi.fn> {
  let idx = 0;
  const mock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => {
    const spec = specs[Math.min(idx, specs.length - 1)]!;
    idx += 1;
    if (spec.urlMatch !== undefined) {
      expect(String(url)).toMatch(spec.urlMatch);
    }
    const text = spec.body !== undefined ? JSON.stringify(spec.body) : "";
    return Promise.resolve({
      ok: spec.status >= 200 && spec.status < 300,
      status: spec.status,
      text: () => Promise.resolve(text),
    } as unknown as Response);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function renderPlay() {
  return render(
    <MemoryRouter initialEntries={[`/play/${GAME_ID}`]}>
      <Routes>
        <Route path="/play/:gameId" element={<PlayPage />} />
        <Route
          path="/results/:gameId"
          element={<div data-testid="results-marker">results</div>}
        />
        <Route
          path="/ready"
          element={<div data-testid="ready-marker">ready</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Tick the countdown three times and flush the ``POST /begin``
 * promise so the page lands in the ``typing`` state with the input
 * mounted. Pattern mirrors the "auto-navigates to /ready after the
 * timeout redirect window" test in ``PlayPage.test.tsx``.
 */
async function driveCountdownAndBegin(): Promise<HTMLInputElement> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  // Flush the microtasks the ``beginning`` effect enqueues when its
  // awaited ``POST /begin`` resolves.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  return screen.getByTestId("play-input") as HTMLInputElement;
}

// ---------------------------------------------------------------------------
// Suite state
// ---------------------------------------------------------------------------

// The ``describe`` block below sets up fake timers and the session
// token once per iteration inside the property callback rather than
// in ``beforeEach``. Fast-check drives many iterations inside a
// single ``it``, so per-iteration setup must live in the property
// body; the outer hooks are only a final safety net.
beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Property 7 — no network request during typing
// ---------------------------------------------------------------------------

/**
 * Generator for a single incomplete typed input. ``unit: "grapheme"``
 * matches the generator in the sibling property-test suites; the
 * ``maxLength: PROMPT.length - 1`` cap enforces the "strictly less
 * than the prompt length" invariant that keeps the onChange handler
 * from transitioning into ``submitting`` and firing ``POST /result``.
 *
 * Note: ``maxLength`` caps the *grapheme* count, but a grapheme may
 * occupy multiple UTF-16 code units, so a generated string can have
 * ``len(u_j) >= PROMPT.length`` in UTF-16 terms. That's still
 * harmless for this property — the completion check in
 * :func:`handleInput` also counts UTF-16 code units, but it
 * transitions to ``submitting`` only when the capped length *equals*
 * the prompt length (i.e., the paste filled or overflowed). For a
 * grapheme-counted input of at most ``PROMPT.length - 1`` graphemes
 * that happens to expand to a long UTF-16 string, the onChange
 * handler slices to ``PROMPT.length`` and that path does transition
 * to ``submitting`` and fires ``/result``. We therefore additionally
 * filter on UTF-16 length below so the property holds exactly as
 * stated in ``design.md``: ``len(u_j) < len(prompt)``.
 */
const incompleteTypedArb = fc
  .string({ unit: "grapheme", maxLength: PROMPT.length - 1 })
  .filter((s) => s.length < PROMPT.length);

const typedSequenceArb = fc.array(incompleteTypedArb, {
  minLength: 1,
  maxLength: 10,
});

describe("Feature: typing-input-highlighting, Property 7: No network request is issued during typing", () => {
  it("fires exactly one fetch (POST /begin) across an arbitrary sequence of incomplete typed inputs (Requirements 6.1, 6.2)", async () => {
    await fc.assert(
      fc.asyncProperty(typedSequenceArb, async (typedSequence) => {
        // -----------------------------------------------------------
        // Per-iteration setup: every iteration must start from a
        // clean slate because fast-check runs the property callback
        // inside the single ``it`` block.
        // -----------------------------------------------------------
        window.localStorage.clear();
        window.sessionStorage.clear();
        window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
        vi.unstubAllGlobals();
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

        const { unmount } = renderPlay();
        try {
          // Drive the countdown + ``POST /begin`` handshake so only
          // the begin call has fired before the first keystroke.
          const input = await driveCountdownAndBegin();

          // Guard the post-handshake invariant: exactly one fetch
          // for ``POST /begin`` before any typing. If the countdown
          // path itself starts to issue other requests this guard
          // will fail fast with a clearer message than the final
          // assertion.
          expect(fetchMock).toHaveBeenCalledTimes(1);
          expect(String(fetchMock.mock.calls[0]![0])).toMatch(
            /\/games\/g1\/begin$/,
          );

          // -----------------------------------------------------------
          // Drive the sequence of incomplete typed inputs u_1, …, u_k.
          // Every u_j satisfies ``len(u_j) < PROMPT.length`` so the
          // onChange handler never transitions into ``submitting`` —
          // i.e., ``POST /result`` is never fired. If any change
          // somehow fires an extra fetch the assertion below will
          // catch it.
          // -----------------------------------------------------------
          for (const uj of typedSequence) {
            // Belt-and-suspenders: strictly less than the prompt
            // length in UTF-16 code units, which is how
            // ``handleInput`` measures completion.
            expect(uj.length).toBeLessThan(PROMPT.length);
            await act(async () => {
              fireEvent.change(input, { target: { value: uj } });
            });
            // Flush any microtasks the render might schedule. The
            // render path does not itself schedule fetches, but
            // advancing the fake timer by zero is the cheapest way
            // to observe steady state and gives the property its
            // "no delayed fetch either" guarantee.
            await act(async () => {
              await vi.advanceTimersByTimeAsync(0);
            });
          }

          // -----------------------------------------------------------
          // Property 7: exactly one fetch across the entire sequence.
          // -----------------------------------------------------------
          expect(fetchMock).toHaveBeenCalledTimes(1);
          expect(String(fetchMock.mock.calls[0]![0])).toMatch(
            /\/games\/g1\/begin$/,
          );
        } finally {
          // Clean up before the next iteration. Unmounting first
          // avoids React's effect cleanup running against a set of
          // stubs that the next iteration is about to replace.
          unmount();
          cleanup();
          vi.unstubAllGlobals();
          vi.useRealTimers();
          window.sessionStorage.clear();
          window.localStorage.clear();
        }
      }),
      { numRuns: 100 },
    );
  });
});
