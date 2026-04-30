/**
 * Example component tests for :func:`TypingView` — one per
 * Character_State (typing-input-highlighting task 3.2).
 *
 * These four curated cases verify that each Character_State's
 * render contract holds at the index of interest:
 *
 *   * ``data-char-class`` equals the expected enum value
 *     (Requirement 2.5, 7.2).
 *   * ``className`` contains ``char--<state>`` so the stylesheet
 *     can paint it (Requirements 2.1–2.4).
 *   * ``aria-current="true"`` appears on exactly the ``current``
 *     span and is absent everywhere else (Requirements 3.1, 3.3,
 *     7.3).
 *
 * This is the *example* counterpart to the property-based tests in
 * :file:`TypingView.pbt.test.tsx`. The design explicitly calls for
 * example tests here rather than a fifth property: the contract is a
 * four-valued enum, so 100 fast-check iterations would repeat the
 * same four assertions.
 *
 * Render harness:
 *   * Each test synthesizes a ``typing``-kind
 *     :type:`TypingPageState` directly — no countdown, no
 *     ``POST /begin``, no fetch plumbing — and mounts
 *     :func:`TypingView` in isolation. This mirrors the PBT harness
 *     in :file:`TypingView.pbt.test.tsx`.
 *   * ``onChange`` is a no-op; these tests inspect the rendered DOM
 *     derived from ``(state.prompt, state.typed)``, not the keystroke
 *     plumbing (covered by Property 6 in task 5.2).
 */

import { afterEach, describe, expect, it } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { createRef } from "react";

import { TypingView, type TypingPageState } from "./PlayPage";

// ---------------------------------------------------------------------------
// Render harness (mirrors :file:`TypingView.pbt.test.tsx`)
// ---------------------------------------------------------------------------

function makeTypingState(prompt: string, typed: string): TypingPageState {
  return {
    kind: "typing",
    prompt,
    typed,
    startedAtMs: 0,
    startedAtServer: "2030-01-01T00:00:00Z",
  };
}

function renderTypingView(prompt: string, typed: string) {
  const state = makeTypingState(prompt, typed);
  const inputRef = createRef<HTMLInputElement>();
  return render(
    <TypingView
      state={state}
      onChange={() => {
        /* no-op — these tests assert on the rendered DOM only */
      }}
      inputRef={inputRef}
    />,
  );
}

function getCharSpan(
  container: ParentNode,
  index: number,
): HTMLElement {
  const el = container.querySelector<HTMLElement>(
    `[data-testid="play-char-${index}"]`,
  );
  if (el === null) {
    throw new Error(`Character_Display at index ${index} not found`);
  }
  return el;
}

function getAllCharSpans(container: ParentNode): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      '[data-testid="play-prompt"] > span',
    ),
  );
}

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Example tests — one per Character_State
// ---------------------------------------------------------------------------

describe("TypingView Character_State render contract", () => {
  it("renders the Character_Display at the Cursor_Position with the `current` contract when typed is empty (Requirements 2.5, 3.1, 7.3)", () => {
    // prompt="abc", typed="" → index 0 is the Cursor_Position, so it
    // is ``current``; indices 1 and 2 are pending.
    const { container } = renderTypingView("abc", "");

    const char0 = getCharSpan(container, 0);
    expect(char0.getAttribute("data-char-class")).toBe("current");
    expect(char0.className.split(/\s+/)).toContain("char--current");
    expect(char0.getAttribute("aria-current")).toBe("true");

    for (const i of [1, 2]) {
      const other = getCharSpan(container, i);
      expect(other.getAttribute("data-char-class")).toBe("pending");
      expect(other.className.split(/\s+/)).toContain("char--pending");
      // Requirement 7.3 / design Component 3: ``aria-current`` must be
      // absent from non-current spans (not ``"false"`` or empty — the
      // attribute itself is omitted).
      expect(other.hasAttribute("aria-current")).toBe(false);
    }

    // And exactly one element in the subtree carries
    // ``aria-current="true"`` (Requirement 3.1).
    const ariaCurrentEls = container.querySelectorAll('[aria-current="true"]');
    expect(ariaCurrentEls.length).toBe(1);
  });

  it("renders a correctly typed Character_Display with the `correct` contract (Requirements 2.1, 2.5, 7.3)", () => {
    // prompt="abc", typed="a" → index 0 is ``correct``; index 1 is
    // the new Cursor_Position so it is ``current``.
    const { container } = renderTypingView("abc", "a");

    const char0 = getCharSpan(container, 0);
    expect(char0.getAttribute("data-char-class")).toBe("correct");
    expect(char0.className.split(/\s+/)).toContain("char--correct");
    expect(char0.hasAttribute("aria-current")).toBe(false);

    // Sanity: the cursor has moved to index 1 (Requirement 3.1).
    const char1 = getCharSpan(container, 1);
    expect(char1.getAttribute("data-char-class")).toBe("current");
    expect(char1.getAttribute("aria-current")).toBe("true");

    const ariaCurrentEls = container.querySelectorAll('[aria-current="true"]');
    expect(ariaCurrentEls.length).toBe(1);
  });

  it("renders a mistyped Character_Display with the `incorrect` contract (Requirements 2.2, 2.5, 7.3)", () => {
    // prompt="abc", typed="X" → index 0 is ``incorrect`` (a Typo);
    // index 1 is ``current``.
    const { container } = renderTypingView("abc", "X");

    const char0 = getCharSpan(container, 0);
    expect(char0.getAttribute("data-char-class")).toBe("incorrect");
    expect(char0.className.split(/\s+/)).toContain("char--incorrect");
    expect(char0.hasAttribute("aria-current")).toBe(false);

    const char1 = getCharSpan(container, 1);
    expect(char1.getAttribute("data-char-class")).toBe("current");
    expect(char1.getAttribute("aria-current")).toBe("true");

    const ariaCurrentEls = container.querySelectorAll('[aria-current="true"]');
    expect(ariaCurrentEls.length).toBe(1);
  });

  it("renders a completed prompt with every Character_Display marked `correct` and no `current` span (Requirements 2.1, 2.5, 3.3, 7.3)", () => {
    // prompt="abc", typed="abc" → every index is ``correct``; the
    // cursor has moved past the end of the prompt, so no element
    // carries ``aria-current="true"`` (Requirement 3.3 /
    // Property 4).
    const { container } = renderTypingView("abc", "abc");

    const spans = getAllCharSpans(container);
    expect(spans.length).toBe(3);
    for (const span of spans) {
      expect(span.getAttribute("data-char-class")).toBe("correct");
      expect(span.className.split(/\s+/)).toContain("char--correct");
      expect(span.hasAttribute("aria-current")).toBe(false);
    }

    // No element in the rendered tree carries ``aria-current="true"``
    // once the prompt is complete (Requirement 3.3).
    const ariaCurrentEls = container.querySelectorAll('[aria-current="true"]');
    expect(ariaCurrentEls.length).toBe(0);
  });
});
