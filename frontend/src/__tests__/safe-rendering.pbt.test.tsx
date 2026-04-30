/**
 * Property-based test for safe rendering of untrusted text (task 12.9).
 *
 * **Property 17: Safe rendering of untrusted text**
 * **Validates: Requirements 13.1, 13.2**
 *
 * For any nickname or typed content, the Web_Client SHALL render the
 * text in a manner that does not allow the content to be interpreted
 * as executable HTML or script. We verify this structurally against
 * the rendered DOM rather than (e.g.) watching for script execution
 * because jsdom does not evaluate ``<script>`` nodes even when they
 * are injected, so a behavioural check would accept broken markup
 * that still contains a script element.
 *
 * The two surfaces covered are the two places Requirement 13 calls
 * out:
 *   * **Nickname** — ``ResultsPage`` reads it from ``localStorage`` via
 *     :func:`readStoredNickname` and interpolates it inside a
 *     ``<strong>`` element. (Requirement 13.1)
 *   * **Typed content** — ``PlayPage`` renders the prompt per-character
 *     through :func:`classifyView`, splitting the string and
 *     interpolating each code point inside its own ``<span>``. We
 *     exercise the same rendering pattern via a minimal harness
 *     ``TypedContentHarness`` so the property test doesn't have to
 *     drive the full countdown + typing state machine just to
 *     produce the DOM of interest. (Requirement 13.2)
 *
 * For each generated string we assert:
 *   * ``container.querySelector("script")`` is ``null`` — React's
 *     text interpolation must not produce a script element, no
 *     matter what the string contains.
 *   * No element in the rendered tree carries an event-handler
 *     attribute whose value originated in the untrusted string. We
 *     scan every element for attributes starting with ``on`` (the
 *     HTML event-handler family: ``onclick``, ``onerror``,
 *     ``onload``, ``onmouseover``, etc.); any such attribute is an
 *     injection.
 *
 * fast-check is used with :code:`fc.fullUnicodeString` so we cover
 * a much broader input space than ASCII while keeping the run time
 * bounded (``numRuns: 50``). A handful of explicit injection
 * payloads are also mixed in via :code:`fc.constantFrom` so the
 * property test always exercises the known-scary shapes alongside
 * the random inputs.
 */

import { createRef } from "react";
import { describe, expect, it, beforeEach, afterEach } from "vitest";
import fc from "fast-check";
import { render, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ResultsPage } from "../pages/ResultsPage";
import {
  TypingView,
  classifyView,
  persistResultHandoff,
  type CharacterState,
  type ResultHandoff,
  type TypingPageState,
} from "../pages/PlayPage";
import { NICKNAME_KEY } from "../api/client";

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * Known injection payloads. fast-check's random unicode generator
 * rarely lands on these exact shapes, so mix them in explicitly to
 * guarantee the property test always exercises them.
 */
const INJECTION_PAYLOADS: readonly string[] = [
  "<script>alert(1)</script>",
  "<img src=x onerror=alert(1)>",
  '"><script>evil()</script>',
  "<svg/onload=alert(1)>",
  "<a href=\"javascript:alert(1)\">click</a>",
  "<iframe src=javascript:alert(1)></iframe>",
  "</strong><script>alert(1)</script><strong>",
  "<body onload=alert(1)>",
  "<div onclick=\"alert(1)\">x</div>",
  "<input onfocus=alert(1) autofocus>",
];

/**
 * Arbitrary that blends random full-unicode strings with the known
 * injection payloads so every run covers both the fuzzed space and
 * the adversarial examples. ``unit: "grapheme"`` in fast-check v4
 * produces strings of arbitrary graphemes (full-unicode), replacing
 * the pre-v4 ``fullUnicodeString`` helper.
 */
const untrustedText = fc.oneof(
  { arbitrary: fc.constantFrom(...INJECTION_PAYLOADS), weight: 1 },
  {
    arbitrary: fc.string({ unit: "grapheme", maxLength: 64 }),
    weight: 3,
  },
);

// ---------------------------------------------------------------------------
// DOM safety assertions
// ---------------------------------------------------------------------------

/**
 * HTML event-handler attributes are the family whose names start with
 * ``on``. React's JSX interpolation should never emit one from a
 * plain text value, so any element carrying an ``on*`` attribute is
 * evidence of attribute injection.
 *
 * A couple of edge-case names that start with ``on`` but are not
 * event handlers (``onlyChild`` etc.) don't appear as HTML
 * attributes and would be harmless even if they did; the scan is
 * intentionally permissive about the suffix because we care about
 * the shape of the attack surface, not a fixed allow-list.
 */
function findInjectedEventHandlerAttributes(
  root: ParentNode,
): { element: string; attribute: string }[] {
  const findings: { element: string; attribute: string }[] = [];
  const elements = root.querySelectorAll("*");
  for (const el of Array.from(elements)) {
    for (const attr of Array.from(el.attributes)) {
      if (attr.name.toLowerCase().startsWith("on")) {
        findings.push({ element: el.tagName, attribute: attr.name });
      }
    }
  }
  return findings;
}

/**
 * Assert the rendered DOM of the given container does not contain
 * any injected script nodes or event-handler attributes. Bundled up
 * here so both the nickname and the typed-content tests share one
 * authoritative definition of "safe".
 */
function assertNoInjectionIn(container: ParentNode): void {
  expect(container.querySelector("script")).toBeNull();
  // Also guard against the other classic HTML-injection vectors
  // that React's text interpolation must not produce from a string.
  expect(container.querySelector("iframe")).toBeNull();
  expect(container.querySelector("object")).toBeNull();
  expect(container.querySelector("embed")).toBeNull();

  const injected = findInjectedEventHandlerAttributes(container);
  expect(injected).toEqual([]);
}

// ---------------------------------------------------------------------------
// Test harness: typed-content rendering that mirrors PlayPage
// ---------------------------------------------------------------------------

/**
 * Minimal component that reproduces the exact rendering pattern
 * :class:`PlayPage` uses for the prompt (per-character ``<span>``
 * with a ``data-char-class`` attribute) so the property test can
 * exercise typed content without driving the full countdown +
 * begin state machine.
 *
 * Keeping the harness a faithful copy of PlayPage's JSX — including
 * the ``split("").map`` and the ``classifyView`` call — means that
 * a future change to PlayPage's rendering would be caught if the
 * same change didn't also show up here (or if this harness drifts
 * the property becomes weaker, which is noisy enough to notice).
 */
function TypedContentHarness({
  prompt,
  typed,
}: {
  prompt: string;
  typed: string;
}): JSX.Element {
  const states: readonly CharacterState[] = classifyView(
    prompt,
    typed,
  ).states;
  return (
    <p data-testid="harness-prompt" aria-label="Prompt">
      {prompt.split("").map((ch, i) => (
        <span
          key={i}
          data-testid={`harness-char-${i}`}
          data-char-class={states[i]}
          className={`char char--${states[i]}`}
        >
          {ch}
        </span>
      ))}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Test harness: full TypingView render (Feature: typing-input-highlighting,
// Property 9 — exercises both the no-typo and has-typo branches including
// the aggregate typo indicator)
// ---------------------------------------------------------------------------

/**
 * Build a synthetic ``typing``-kind :type:`TypingPageState`. Mirrors
 * the helper in :file:`frontend/src/pages/TypingView.pbt.test.tsx`:
 * :func:`TypingView` reads only ``prompt`` and ``typed`` from the
 * state, so the two timestamp fields are inert constants.
 */
function makeTypingState(prompt: string, typed: string): TypingPageState {
  return {
    kind: "typing",
    prompt,
    typed,
    startedAtMs: 0,
    startedAtServer: "2030-01-01T00:00:00Z",
  };
}

/**
 * Render the real :func:`TypingView` with a synthetic ``(prompt,
 * typed)`` pair and return the Testing Library handle. Using the
 * production component directly — rather than a JSX-mirroring
 * harness — means the property test exercises the same DOM the
 * player sees, including the per-character ``<span>`` tree, the
 * prompt container, and the aggregate typo indicator (typing-input-
 * highlighting design Component 5 / Property 9).
 */
function renderTypingViewHarness(prompt: string, typed: string) {
  const state = makeTypingState(prompt, typed);
  const inputRef = createRef<HTMLInputElement>();
  return render(
    <TypingView
      state={state}
      onChange={() => {
        /* no-op — Property 9 is a pure-view safe-rendering assertion */
      }}
      inputRef={inputRef}
    />,
  );
}

// ---------------------------------------------------------------------------
// ResultsPage helpers
// ---------------------------------------------------------------------------

const GAME_ID = "pbt-game";

function makeHandoff(): ResultHandoff {
  return {
    gameId: GAME_ID,
    wpm: 42.5,
    accuracy: 100,
    points: 425,
    rank: 1,
    endedAt: "2030-01-01T00:00:10Z",
    clientElapsedMs: 10_000,
  };
}

function renderResultsWithNickname(nickname: string) {
  window.localStorage.setItem(NICKNAME_KEY, nickname);
  persistResultHandoff(makeHandoff());
  return render(
    <MemoryRouter initialEntries={[`/results/${GAME_ID}`]}>
      <Routes>
        <Route path="/results/:gameId" element={<ResultsPage />} />
        <Route path="/ready" element={<div>ready</div>} />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

describe("Property 17: Safe rendering of untrusted text", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders arbitrary nicknames on the Results page without injecting script nodes or event-handler attributes (Requirement 13.1)", () => {
    fc.assert(
      fc.property(untrustedText, (nickname) => {
        const { container, unmount } = renderResultsWithNickname(nickname);
        try {
          assertNoInjectionIn(container);
        } finally {
          unmount();
          window.localStorage.clear();
          window.sessionStorage.clear();
        }
      }),
      { numRuns: 50 },
    );
  });

  it("renders arbitrary prompts character-by-character without injecting script nodes or event-handler attributes (Requirement 13.2)", () => {
    fc.assert(
      fc.property(untrustedText, (prompt) => {
        const { container, unmount } = render(
          <TypedContentHarness prompt={prompt} typed="" />,
        );
        try {
          assertNoInjectionIn(container);
        } finally {
          unmount();
        }
      }),
      { numRuns: 50 },
    );
  });

  it("renders arbitrary typed content on top of arbitrary prompts without injection (Requirement 13.2)", () => {
    // Exercise the full typed-content rendering path where both the
    // prompt and the typed string are untrusted. The per-character
    // span rendering is identical to PlayPage's, so any injection
    // surface in PlayPage's typing view would show up here too.
    fc.assert(
      fc.property(untrustedText, untrustedText, (prompt, typed) => {
        const { container, unmount } = render(
          <TypedContentHarness prompt={prompt} typed={typed} />,
        );
        try {
          assertNoInjectionIn(container);
        } finally {
          unmount();
        }
      }),
      { numRuns: 50 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 9 (typing-input-highlighting): Safe rendering of highlighted
// characters.
//
// Feature: typing-input-highlighting, Property 9: Safe rendering of
// highlighted characters. Requirement 8.1 covers the per-character
// Character_Display render. Property 17 above already exercises the
// per-character render path via the JSX-mirroring
// ``TypedContentHarness``; this block renders the real
// :func:`TypingView` and asserts safety across both no-typo and
// has-typo input branches.
// ---------------------------------------------------------------------------

describe("Property 9: Safe rendering of highlighted characters (typing-input-highlighting, Requirement 8.1)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders the real TypingView safely in the no-typo branch", () => {
    fc.assert(
      fc.property(
        untrustedText,
        fc.double({ min: 0, max: 1, noNaN: true }),
        (prompt, prefixFraction) => {
          const cut = Math.floor(prompt.length * prefixFraction);
          const typed = prompt.slice(0, cut);

          const { container, unmount } = renderTypingViewHarness(
            prompt,
            typed,
          );
          try {
            assertNoInjectionIn(container);
          } finally {
            unmount();
          }
        },
      ),
      { numRuns: 50 },
    );
  });

  it("renders the real TypingView safely in the has-typo branch", () => {
    const nonEmptyUntrustedText = untrustedText.filter((s) => s.length > 0);

    fc.assert(
      fc.property(
        nonEmptyUntrustedText,
        fc.double({ min: 0, max: 1, noNaN: true }),
        fc.double({ min: 0, max: 1, noNaN: true }),
        (prompt, lengthFraction, typoIndexFraction) => {
          const baseLen = Math.max(
            1,
            Math.ceil(prompt.length * lengthFraction),
          );
          const baseTyped = prompt.slice(0, baseLen);
          const typoIdx = Math.min(
            baseLen - 1,
            Math.floor(baseLen * typoIndexFraction),
          );
          const original = baseTyped[typoIdx] ?? "";
          const replacement = original === "X" ? "Y" : "X";
          const typed =
            baseTyped.slice(0, typoIdx) +
            replacement +
            baseTyped.slice(typoIdx + 1);

          const { container, unmount } = renderTypingViewHarness(
            prompt,
            typed,
          );
          try {
            assertNoInjectionIn(container);
          } finally {
            unmount();
          }
        },
      ),
      { numRuns: 50 },
    );
  });
});
