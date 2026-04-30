/**
 * Property-based test for the Dashboard (task 13.4).
 *
 * **Property 16: Dashboard render contains required fields**
 * **Validates: Requirement 6.1**
 *
 *   *For any* Leaderboard snapshot, the Dashboard_Client's rendered
 *   top-N rows SHALL include the ``nickname``, ``bestWpm``,
 *   ``bestAccuracy``, and ``bestPoints`` for each displayed
 *   LeaderboardEntry.
 *
 * Strategy:
 *   * Generate arbitrary :class:`LeaderboardSnapshot` payloads with
 *     fast-check — ``playerId`` is unique per entry (so React's
 *     ``key`` invariant holds), ``nickname`` spans the full unicode
 *     space (including the adversarial injection payloads used
 *     elsewhere in the suite), and the three numeric fields use
 *     realistic ranges but are free to take any finite double.
 *   * Install a fetch stub that returns the generated snapshot, mount
 *     :class:`DashboardPage`, flush the initial poll, then for every
 *     entry in the generated snapshot assert that the matching row
 *     text contains each required field formatted through the same
 *     helpers (:func:`formatWpm` etc.) the component uses. We key by
 *     ``playerId`` so the assertion can't accidentally pass from an
 *     unrelated row.
 *   * fast-check runs with ``numRuns: 25`` so the test finishes in
 *     well under a second on CI while still exercising a broad
 *     slice of the input space.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fc from "fast-check";
import { act, cleanup, render, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import {
  DashboardPage,
  formatAccuracy,
  formatPoints,
  formatWpm,
} from "../pages/DashboardPage";
import type { LeaderboardEntry } from "../api/client";

// ---------------------------------------------------------------------------
// Fetch stub
// ---------------------------------------------------------------------------

function stubFetchWithSnapshot(snapshot: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(snapshot)),
      } as unknown as Response),
    ),
  );
}

/** Flush pending microtasks so React settles the fetch → setState chain. */
async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * Arbitrary nickname. Uses full-unicode strings so the property
 * exercises adversarial shapes (HTML-looking, RTL marks, etc.)
 * alongside the plain cases.
 */
const arbNickname = fc.string({ unit: "grapheme", minLength: 1, maxLength: 20 });

/** Arbitrary ``bestWpm`` — non-negative finite double. */
const arbWpm = fc
  .double({ min: 0, max: 1000, noNaN: true })
  .filter((v) => Number.isFinite(v));

/** Arbitrary ``bestAccuracy`` — percentage in ``[0, 100]``. */
const arbAccuracy = fc
  .double({ min: 0, max: 100, noNaN: true })
  .filter((v) => Number.isFinite(v));

/** Arbitrary ``bestPoints`` — non-negative integer. */
const arbPoints = fc.integer({ min: 0, max: 1_000_000 });

/**
 * Arbitrary snapshot: a list of entries with unique ``playerId``.
 * We build the list by generating a set of ids first, then zipping
 * them with per-entry arbitraries so two entries can't collide.
 */
const arbSnapshot = fc
  .uniqueArray(
    fc.string({ unit: "grapheme", minLength: 1, maxLength: 8 }),
    { minLength: 0, maxLength: 10 },
  )
  .chain((playerIds) =>
    fc.tuple(
      ...playerIds.map((playerId) =>
        fc
          .record({
            nickname: arbNickname,
            bestWpm: arbWpm,
            bestAccuracy: arbAccuracy,
            bestPoints: arbPoints,
          })
          .map((partial) => ({ ...partial, playerId })),
      ),
    ),
  )
  .map((entries): { entries: LeaderboardEntry[]; generatedAt: string } => ({
    entries: entries.map((e, i) => ({
      playerId: e.playerId,
      nickname: e.nickname,
      bestWpm: e.bestWpm,
      bestAccuracy: e.bestAccuracy,
      bestPoints: e.bestPoints,
      rank: i + 1,
    })),
    generatedAt: "2030-01-01T00:00:00Z",
  }));

// ---------------------------------------------------------------------------
// Property
// ---------------------------------------------------------------------------

describe("Property 16: Dashboard render contains required fields", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it("renders nickname, bestWpm, bestAccuracy, bestPoints for every entry in the snapshot (Requirement 6.1)", async () => {
    await fc.assert(
      fc.asyncProperty(arbSnapshot, async (snapshot) => {
        stubFetchWithSnapshot(snapshot);

        const { container, unmount } = render(
          <MemoryRouter initialEntries={["/dashboard"]}>
            <DashboardPage />
          </MemoryRouter>,
        );

        try {
          await flushMicrotasks();

          if (snapshot.entries.length === 0) {
            // No rows to assert against; the component shows the
            // empty-state hint. The property is vacuously satisfied.
            expect(
              container.querySelector('[data-testid="dashboard-empty"]'),
            ).not.toBeNull();
            return;
          }

          for (const entry of snapshot.entries) {
            const row = container.querySelector(
              // ``playerId`` can contain characters that are
              // unsafe inside a CSS selector (quotes, backslashes,
              // newlines, surrogate halves, etc.). Scope the lookup
              // to the tbody and iterate instead of escaping.
              `[data-testid="dashboard-table"] tbody`,
            );
            expect(row).not.toBeNull();
            const tbody = row as HTMLElement;

            // Find the row whose rank cell matches this entry's
            // rank — ranks are unique in our generator, so this
            // uniquely identifies the row.
            const rows = Array.from(tbody.querySelectorAll("tr"));
            const match = rows.find((tr) => {
              const rankCell = tr.querySelector(
                '[data-testid="dashboard-cell-rank"]',
              );
              return rankCell?.textContent === String(entry.rank);
            });
            expect(match).toBeDefined();
            const rowEl = match!;

            // ``within`` lets us scope the text assertions and
            // avoids false positives from sibling rows.
            const rowScope = within(rowEl);

            // Nickname is rendered verbatim via text interpolation.
            expect(
              rowScope.getByTestId("dashboard-cell-nickname").textContent,
            ).toBe(entry.nickname);

            // Numeric fields are rendered through the same helpers
            // the component uses, so the property-test oracle
            // matches the implementation exactly.
            expect(
              rowScope.getByTestId("dashboard-cell-wpm").textContent,
            ).toBe(formatWpm(entry.bestWpm));
            expect(
              rowScope.getByTestId("dashboard-cell-accuracy").textContent,
            ).toBe(formatAccuracy(entry.bestAccuracy));
            expect(
              rowScope.getByTestId("dashboard-cell-points").textContent,
            ).toBe(formatPoints(entry.bestPoints));
          }
        } finally {
          unmount();
          vi.unstubAllGlobals();
        }
      }),
      { numRuns: 25 },
    );
  });
});
