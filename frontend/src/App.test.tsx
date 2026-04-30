import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "./App";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App router", () => {
  beforeEach(() => {
    // Stub fetch with a never-resolving promise so pages that kick
    // off a request on mount (e.g. PlayPage's fallback to
    // ``GET /games/{id}``) don't produce act() warnings after the
    // synchronous assertions land.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the nickname page at /", () => {
    renderAt("/");
    expect(screen.getByTestId("nickname-input")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /typing game/i })).toBeInTheDocument();
  });

  it("renders the ready page at /ready", () => {
    renderAt("/ready");
    expect(screen.getByTestId("ready-placeholder")).toBeInTheDocument();
  });

  it("renders the play page at /play/:gameId", () => {
    renderAt("/play/abc123");
    // The PlayPage starts in a loading state while it fetches or
    // reads the game prompt; asserting the loading surface confirms
    // the route wiring without depending on network stubs.
    expect(screen.getByTestId("play-loading")).toBeInTheDocument();
  });

  it("renders the results page at /results/:gameId", () => {
    // No hand-off has been persisted in sessionStorage for this
    // gameId, so the Results page renders its missing-hand-off
    // fallback. That surface is enough to confirm the route wiring.
    renderAt("/results/xyz789");
    expect(screen.getByTestId("results-missing")).toBeInTheDocument();
    expect(screen.getByTestId("results-play-again")).toBeInTheDocument();
  });

  it("renders the dashboard page at /dashboard", () => {
    // The DashboardPage fires ``GET /leaderboard`` on mount (task
    // 13.1); since ``fetch`` is stubbed with a never-resolving
    // promise in ``beforeEach``, the page renders its initial
    // loading state. Asserting that surface is enough to confirm
    // the route wiring — tests in ``DashboardPage.test.tsx``
    // exercise the fetch / render behavior directly.
    renderAt("/dashboard");
    expect(screen.getByTestId("dashboard-loading")).toBeInTheDocument();
  });
});
