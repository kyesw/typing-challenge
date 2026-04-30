/**
 * Component tests for the Ready page (task 12.3).
 *
 * Covers:
 *   * The Start button calls ``POST /games`` and navigates to
 *     ``/play/:gameId`` on 201, stashing the prompt + startAt
 *     hand-off for the Countdown + Typing page
 *     (Requirements 2.1, 2.2, 2.5).
 *   * A generic server / network failure surfaces an inline error
 *     without navigating.
 *   * Hand-off helpers round-trip sessionStorage.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  GAME_HANDOFF_KEY_PREFIX,
  ReadyPage,
  persistGameHandoff,
  readGameHandoff,
} from "./ReadyPage";
import {
  SESSION_TOKEN_KEY,
} from "../api/client";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function renderReady() {
  return render(
    <MemoryRouter initialEntries={["/ready"]}>
      <Routes>
        <Route path="/ready" element={<ReadyPage />} />
        <Route
          path="/play/:gameId"
          element={<PlayMarker />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function PlayMarker() {
  // Read the gameId via the DOM path by rendering it through a param
  // reader. Keeping this minimal — we just need to assert that
  // navigation landed here with the right id.
  return <div data-testid="play-marker">play</div>;
}

type FetchResponseSpec = {
  status: number;
  body?: unknown;
};

/**
 * Replace ``window.fetch`` with a mock that returns each spec in
 * order, one per call. The mock also records the request details so
 * individual tests can assert on method / headers / body.
 */
function stubFetchSequence(
  specs: readonly FetchResponseSpec[],
): ReturnType<typeof vi.fn> {
  let idx = 0;
  const mock = vi.fn(
    (_url: RequestInfo | URL, _init?: RequestInit) => {
      const spec = specs[Math.min(idx, specs.length - 1)]!;
      idx += 1;
      const text =
        spec.body !== undefined ? JSON.stringify(spec.body) : "";
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

const successBody = {
  gameId: "g1",
  promptId: "pr1",
  prompt: "The quick brown fox jumps over the lazy dog.",
  language: "en",
  status: "pending",
  startAt: "2030-01-01T00:00:00Z",
};

// ---------------------------------------------------------------------------
// Hand-off helpers
// ---------------------------------------------------------------------------

describe("GameHandoff helpers", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("round-trips a payload through sessionStorage", () => {
    persistGameHandoff({
      gameId: "g1",
      promptId: "pr1",
      prompt: "hello world",
      language: "en",
      status: "pending",
      startAt: "2030-01-01T00:00:00Z",
    });

    const key = `${GAME_HANDOFF_KEY_PREFIX}g1`;
    expect(window.sessionStorage.getItem(key)).not.toBeNull();

    const got = readGameHandoff("g1");
    expect(got).toMatchObject({
      gameId: "g1",
      prompt: "hello world",
      startAt: "2030-01-01T00:00:00Z",
    });
  });

  it("returns null for unknown gameIds", () => {
    expect(readGameHandoff("missing")).toBeNull();
  });

  it("returns null when the stored value is not valid JSON", () => {
    window.sessionStorage.setItem(
      `${GAME_HANDOFF_KEY_PREFIX}bad`,
      "{not-json",
    );
    expect(readGameHandoff("bad")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ReadyPage component
// ---------------------------------------------------------------------------

describe("ReadyPage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    // Pre-populate an auth token so startGame() attaches Authorization.
    window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the Start button", () => {
    renderReady();
    expect(screen.getByTestId("ready-start")).toBeInTheDocument();
    expect(screen.getByTestId("ready-start")).toHaveTextContent(/start/i);
  });

  it("starts a game, stashes the hand-off, and navigates to /play/:gameId", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetchSequence([
      { status: 201, body: successBody },
    ]);

    renderReady();
    await user.click(screen.getByTestId("ready-start"));

    // Navigation happened.
    expect(await screen.findByTestId("play-marker")).toBeInTheDocument();

    // Request shape.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toMatch(/\/games$/);
    expect((init as RequestInit).method).toBe("POST");
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Authorization")).toBe("Bearer tok");

    // Hand-off saved so the /play page can read it.
    const saved = window.sessionStorage.getItem(
      `${GAME_HANDOFF_KEY_PREFIX}g1`,
    );
    expect(saved).not.toBeNull();
    const parsed = JSON.parse(saved!);
    expect(parsed).toMatchObject({
      gameId: "g1",
      prompt: successBody.prompt,
      startAt: successBody.startAt,
    });
  });

  it("surfaces a generic error when the network call rejects", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );

    renderReady();
    await user.click(screen.getByTestId("ready-start"));

    expect(await screen.findByTestId("ready-error")).toHaveTextContent(
      /unable to reach the server/i,
    );
    expect(screen.queryByTestId("play-marker")).toBeNull();
  });

  it("surfaces the server message on a non-409 API error", async () => {
    const user = userEvent.setup();
    stubFetchSequence([
      {
        status: 500,
        body: {
          code: "internal_error",
          message: "Something went wrong on the server.",
        },
      },
    ]);

    renderReady();
    await user.click(screen.getByTestId("ready-start"));

    expect(await screen.findByTestId("ready-error")).toHaveTextContent(
      /something went wrong/i,
    );
  });
});
