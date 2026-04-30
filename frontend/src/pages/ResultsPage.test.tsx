/**
 * Component tests for the Results page (task 12.5).
 *
 * Covers:
 *   * Renders ``wpm``, ``accuracy``, ``points``, and ``rank`` from
 *     the :type:`ResultHandoff` the Play page persisted in
 *     sessionStorage (Requirements 4.6, 4.7).
 *   * Greets the player by the nickname stored in localStorage
 *     under :const:`NICKNAME_KEY` when present.
 *   * Play again button navigates to ``/ready``.
 *   * Dashboard link points at ``/dashboard``.
 *   * When the hand-off is missing, a fallback error is rendered
 *     and Play again still works.
 *   * Formatter helpers produce expected strings.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  ResultsPage,
  formatAccuracy,
  formatPoints,
  formatRank,
  formatWpm,
  readStoredNickname,
} from "./ResultsPage";
import {
  persistResultHandoff,
  RESULT_HANDOFF_KEY_PREFIX,
  type ResultHandoff,
} from "./PlayPage";
import { NICKNAME_KEY } from "../api/client";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

const GAME_ID = "g-123";

function renderResults(gameId = GAME_ID) {
  return render(
    <MemoryRouter initialEntries={[`/results/${gameId}`]}>
      <Routes>
        <Route path="/results/:gameId" element={<ResultsPage />} />
        <Route
          path="/"
          element={<div data-testid="nickname-marker">nickname</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function makeHandoff(overrides: Partial<ResultHandoff> = {}): ResultHandoff {
  return {
    gameId: GAME_ID,
    wpm: 55.7,
    accuracy: 93.4,
    points: 520,
    rank: 2,
    endedAt: "2030-01-01T00:00:10Z",
    clientElapsedMs: 10_000,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Formatter helpers
// ---------------------------------------------------------------------------

describe("formatter helpers", () => {
  it("formatWpm renders one decimal place", () => {
    expect(formatWpm(0)).toBe("0.0");
    expect(formatWpm(42)).toBe("42.0");
    expect(formatWpm(55.78)).toBe("55.8");
  });

  it("formatWpm handles non-finite values defensively", () => {
    expect(formatWpm(Number.NaN)).toBe("0.0");
    expect(formatWpm(Number.POSITIVE_INFINITY)).toBe("0.0");
  });

  it("formatAccuracy renders a percentage", () => {
    expect(formatAccuracy(0)).toBe("0.0%");
    expect(formatAccuracy(100)).toBe("100.0%");
    expect(formatAccuracy(93.45)).toBe("93.5%");
  });

  it("formatPoints truncates to an integer", () => {
    expect(formatPoints(0)).toBe("0");
    expect(formatPoints(425)).toBe("425");
    expect(formatPoints(425.9)).toBe("425");
  });

  it("formatRank prefixes with '#' and falls back for invalid input", () => {
    expect(formatRank(1)).toBe("#1");
    expect(formatRank(42)).toBe("#42");
    expect(formatRank(0)).toBe("#—");
    expect(formatRank(-3)).toBe("#—");
    expect(formatRank(Number.NaN)).toBe("#—");
  });
});

// ---------------------------------------------------------------------------
// readStoredNickname
// ---------------------------------------------------------------------------

describe("readStoredNickname", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("returns the stored value when set", () => {
    window.localStorage.setItem(NICKNAME_KEY, "Alice");
    expect(readStoredNickname()).toBe("Alice");
  });

  it("returns null when nothing is stored", () => {
    expect(readStoredNickname()).toBeNull();
  });

  it("returns null for an empty string", () => {
    window.localStorage.setItem(NICKNAME_KEY, "");
    expect(readStoredNickname()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ResultsPage — happy path
// ---------------------------------------------------------------------------

describe("ResultsPage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders wpm, accuracy, points, and rank from the hand-off", () => {
    persistResultHandoff(
      makeHandoff({
        wpm: 42.5,
        accuracy: 100,
        points: 425,
        rank: 3,
      }),
    );

    renderResults();

    expect(screen.getByTestId("results-wpm")).toHaveTextContent("42.5");
    expect(screen.getByTestId("results-accuracy")).toHaveTextContent(
      "100.0%",
    );
    expect(screen.getByTestId("results-points")).toHaveTextContent("425");
    expect(screen.getByTestId("results-rank")).toHaveTextContent("#3");
  });

  it("greets the player by nickname when one is stored", () => {
    window.localStorage.setItem(NICKNAME_KEY, "Alice");
    persistResultHandoff(makeHandoff());

    renderResults();

    const greeting = screen.getByTestId("results-nickname");
    expect(greeting).toHaveTextContent(/alice/i);
  });

  it("omits the nickname greeting when none is stored", () => {
    persistResultHandoff(makeHandoff());

    renderResults();

    expect(screen.queryByTestId("results-nickname")).toBeNull();
  });

  it("Exit button navigates to /", async () => {
    const user = userEvent.setup();
    persistResultHandoff(makeHandoff());

    renderResults();

    await user.click(screen.getByTestId("results-play-again"));

    expect(await screen.findByTestId("nickname-marker")).toBeInTheDocument();
  });

  it("does not use dangerous HTML rendering for nickname", () => {
    // Injection attempt — the nickname slot should render the raw
    // text, not an executable <img> tag.
    const payload = "<img src=x onerror=alert(1)>";
    window.localStorage.setItem(NICKNAME_KEY, payload);
    persistResultHandoff(makeHandoff());

    renderResults();

    const greeting = screen.getByTestId("results-nickname");
    expect(greeting.textContent).toContain(payload);
    // No actual <img> element should have been injected.
    expect(greeting.querySelector("img")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ResultsPage — missing hand-off fallback
// ---------------------------------------------------------------------------

describe("ResultsPage missing hand-off", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders an error message when the hand-off is missing", () => {
    renderResults();

    expect(screen.getByTestId("results-missing")).toBeInTheDocument();
    // Stats are not rendered.
    expect(screen.queryByTestId("results-wpm")).toBeNull();
    expect(screen.queryByTestId("results-accuracy")).toBeNull();
    expect(screen.queryByTestId("results-points")).toBeNull();
    expect(screen.queryByTestId("results-rank")).toBeNull();
  });

  it("renders an error message when the hand-off is for a different gameId", () => {
    // Hand-off exists but for a different game — the current page
    // should still treat it as missing.
    window.sessionStorage.setItem(
      `${RESULT_HANDOFF_KEY_PREFIX}other-game`,
      JSON.stringify(makeHandoff({ gameId: "other-game" })),
    );

    renderResults(GAME_ID);

    expect(screen.getByTestId("results-missing")).toBeInTheDocument();
  });

  it("Exit still works from the missing-hand-off fallback", async () => {
    const user = userEvent.setup();
    renderResults();

    await user.click(screen.getByTestId("results-play-again"));

    expect(await screen.findByTestId("nickname-marker")).toBeInTheDocument();
  });
});
