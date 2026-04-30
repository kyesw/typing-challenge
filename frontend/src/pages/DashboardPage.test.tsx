/**
 * Component tests for the Dashboard page (tasks 13.1, 13.2, 13.3).
 *
 * Covers:
 *   * Initial mount issues ``GET /leaderboard`` and renders the
 *     returned rows with ``nickname`` / ``bestWpm`` /
 *     ``bestAccuracy`` / ``bestPoints`` (Requirement 6.1).
 *   * A second fetch fires after ~1s and the table re-renders from
 *     the new snapshot (Requirement 6.2).
 *   * A failed poll preserves the last successful snapshot on
 *     screen (Requirement 6.3).
 *   * Unmounting the page clears the interval — no further
 *     ``fetch`` calls fire after navigation away.
 *
 * Tests drive the polling cadence with vitest fake timers so they
 * finish instantly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  DashboardPage,
  LEADERBOARD_POLL_INTERVAL_MS,
  formatAccuracy,
  formatPoints,
  formatWpm,
} from "./DashboardPage";

// ---------------------------------------------------------------------------
// Fetch fakery
// ---------------------------------------------------------------------------

/** A single ``fetch`` call response or a thrown network error. */
type FetchStep =
  | { status: number; body?: unknown }
  | { reject: Error };

/**
 * Install a ``fetch`` mock that replays the provided steps in order.
 * After the last step is reached the mock keeps returning it so
 * extra polls during a long test don't blow up.
 */
function stubFetchSequence(
  steps: readonly FetchStep[],
): ReturnType<typeof vi.fn> {
  let idx = 0;
  const mock = vi.fn(
    (_url: RequestInfo | URL, _init?: RequestInit) => {
      const step = steps[Math.min(idx, steps.length - 1)]!;
      idx += 1;
      if ("reject" in step) {
        return Promise.reject(step.reject);
      }
      const text =
        step.body !== undefined ? JSON.stringify(step.body) : "";
      return Promise.resolve({
        ok: step.status >= 200 && step.status < 300,
        status: step.status,
        text: () => Promise.resolve(text),
      } as unknown as Response);
    },
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

// ---------------------------------------------------------------------------
// Snapshot fixtures
// ---------------------------------------------------------------------------

const snapshotA = {
  generatedAt: "2030-01-01T00:00:00Z",
  entries: [
    {
      playerId: "p1",
      nickname: "Alice",
      bestWpm: 72.4,
      bestAccuracy: 99.1,
      bestPoints: 717,
      rank: 1,
    },
    {
      playerId: "p2",
      nickname: "Bob",
      bestWpm: 55.0,
      bestAccuracy: 95.5,
      bestPoints: 525,
      rank: 2,
    },
  ],
};

const snapshotB = {
  generatedAt: "2030-01-01T00:00:01Z",
  entries: [
    {
      playerId: "p2",
      nickname: "Bob",
      bestWpm: 80.0,
      bestAccuracy: 98.0,
      bestPoints: 784,
      rank: 1,
    },
    {
      playerId: "p1",
      nickname: "Alice",
      bestWpm: 72.4,
      bestAccuracy: 99.1,
      bestPoints: 717,
      rank: 2,
    },
  ],
};

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/** Render the dashboard inside a MemoryRouter so route hooks work. */
function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <DashboardPage />
    </MemoryRouter>,
  );
}

/**
 * Let any pending microtasks resolve. ``React`` batches state
 * updates inside ``act``, and an ``await`` on a resolved promise is
 * the simplest way to flush the fetch → setState chain under
 * :func:`vi.useFakeTimers`.
 */
async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

// ---------------------------------------------------------------------------
// Formatting helpers (unit tests)
// ---------------------------------------------------------------------------

describe("DashboardPage formatting helpers", () => {
  it("formats WPM with one decimal place", () => {
    expect(formatWpm(72.44)).toBe("72.4");
    expect(formatWpm(0)).toBe("0.0");
    expect(formatWpm(NaN)).toBe("0.0");
  });

  it("formats accuracy as a percentage", () => {
    expect(formatAccuracy(99.12)).toBe("99.1%");
    expect(formatAccuracy(100)).toBe("100.0%");
    expect(formatAccuracy(Number.POSITIVE_INFINITY)).toBe("0.0%");
  });

  it("formats points as a plain integer string", () => {
    expect(formatPoints(717)).toBe("717");
    expect(formatPoints(717.9)).toBe("717");
    expect(formatPoints(NaN)).toBe("0");
  });
});

// ---------------------------------------------------------------------------
// Dashboard behavior
// ---------------------------------------------------------------------------

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    // Drop any pending timers before swapping back to real ones so
    // an in-flight interval can't leak into the next test.
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it("fetches the initial snapshot on mount and renders rows with the required columns", async () => {
    const fetchMock = stubFetchSequence([
      { status: 200, body: snapshotA },
    ]);

    renderDashboard();

    // The initial fetch fires synchronously during mount.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toMatch(/\/leaderboard$/);
    expect((init as RequestInit | undefined)?.method).toBe("GET");

    // Loading surface is visible until the first poll resolves.
    expect(screen.getByTestId("dashboard-loading")).toBeInTheDocument();

    // Let the fetch promise settle and React flush the state update.
    await flushMicrotasks();

    // Table replaces the loading surface.
    expect(screen.queryByTestId("dashboard-loading")).toBeNull();
    expect(screen.getByTestId("dashboard-table")).toBeInTheDocument();

    // Row content — the four required fields per Requirement 6.1.
    const row = screen.getByTestId("dashboard-row-p1");
    expect(row).toHaveTextContent("Alice");
    expect(row).toHaveTextContent("72.4");
    expect(row).toHaveTextContent("99.1%");
    expect(row).toHaveTextContent("717");

    const row2 = screen.getByTestId("dashboard-row-p2");
    expect(row2).toHaveTextContent("Bob");
    expect(row2).toHaveTextContent("55.0");
    expect(row2).toHaveTextContent("95.5%");
    expect(row2).toHaveTextContent("525");
  });

  it("renders an empty-state hint when the snapshot has no entries", async () => {
    stubFetchSequence([
      {
        status: 200,
        body: { entries: [], generatedAt: "2030-01-01T00:00:00Z" },
      },
    ]);

    renderDashboard();
    await flushMicrotasks();

    expect(screen.getByTestId("dashboard-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-table")).toBeNull();
  });

  it("polls GET /leaderboard every 1s and re-renders each successful snapshot", async () => {
    const fetchMock = stubFetchSequence([
      { status: 200, body: snapshotA },
      { status: 200, body: snapshotB },
    ]);

    renderDashboard();
    await flushMicrotasks();

    // First snapshot: Alice is #1.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("dashboard-row-p1")).toHaveTextContent("Alice");
    expect(screen.getByTestId("dashboard-row-p1")).toHaveTextContent("72.4");

    // Advance one polling interval and flush the resulting fetch.
    await act(async () => {
      vi.advanceTimersByTime(LEADERBOARD_POLL_INTERVAL_MS);
    });
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Second snapshot: Bob is now #1 with updated WPM.
    const bobRow = screen.getByTestId("dashboard-row-p2");
    expect(bobRow).toHaveTextContent("Bob");
    expect(bobRow).toHaveTextContent("80.0");
    expect(bobRow).toHaveTextContent("98.0%");
    expect(bobRow).toHaveTextContent("784");

    // And Alice has dropped to rank 2.
    const aliceRow = screen.getByTestId("dashboard-row-p1");
    expect(aliceRow).toHaveTextContent("2");
  });

  it("keeps the last successful snapshot visible when a poll fails", async () => {
    const fetchMock = stubFetchSequence([
      { status: 200, body: snapshotA },
      { reject: new Error("network down") },
      { reject: new Error("still down") },
    ]);

    renderDashboard();
    await flushMicrotasks();

    // First successful snapshot.
    expect(screen.getByTestId("dashboard-row-p1")).toHaveTextContent("Alice");

    // Two consecutive failed polls.
    await act(async () => {
      vi.advanceTimersByTime(LEADERBOARD_POLL_INTERVAL_MS);
    });
    await flushMicrotasks();
    await act(async () => {
      vi.advanceTimersByTime(LEADERBOARD_POLL_INTERVAL_MS);
    });
    await flushMicrotasks();

    // Three fetch calls total: 1 initial + 2 failing polls.
    expect(fetchMock).toHaveBeenCalledTimes(3);

    // Previous snapshot is still on screen — no blank-out on
    // transient failure (Requirement 6.3).
    expect(screen.getByTestId("dashboard-table")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-row-p1")).toHaveTextContent("Alice");
    expect(screen.getByTestId("dashboard-row-p2")).toHaveTextContent("Bob");

    // And there's no error banner — the dashboard quietly rides out
    // the blip.
    expect(screen.queryByTestId("dashboard-error")).toBeNull();
  });

  it("surfaces an error when the initial snapshot fetch fails", async () => {
    stubFetchSequence([{ reject: new Error("boom") }]);

    renderDashboard();
    await flushMicrotasks();

    expect(screen.getByTestId("dashboard-error")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-table")).toBeNull();
  });

  it("recovers from an initial failure on the next successful poll", async () => {
    stubFetchSequence([
      { reject: new Error("boom") },
      { status: 200, body: snapshotA },
    ]);

    renderDashboard();
    await flushMicrotasks();
    expect(screen.getByTestId("dashboard-error")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(LEADERBOARD_POLL_INTERVAL_MS);
    });
    await flushMicrotasks();

    expect(screen.queryByTestId("dashboard-error")).toBeNull();
    expect(screen.getByTestId("dashboard-row-p1")).toHaveTextContent("Alice");
  });

  it("clears the interval on unmount — no fetch fires after navigation away", async () => {
    // Wrap the dashboard in routes so we can unmount it by
    // navigating. We don't actually need to navigate — calling
    // ``unmount()`` is enough to exercise the cleanup path — but
    // using a router keeps the harness shape identical to the
    // page's production environment.
    const fetchMock = stubFetchSequence([
      { status: 200, body: snapshotA },
    ]);

    const { unmount } = render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();

    // Advance well past the polling cadence. If the interval had
    // leaked, ``fetchMock`` would be called again; the assertion
    // below catches that regression.
    await act(async () => {
      vi.advanceTimersByTime(LEADERBOARD_POLL_INTERVAL_MS * 5);
    });
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
