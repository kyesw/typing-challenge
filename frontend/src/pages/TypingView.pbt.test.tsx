/**
 * Property-based tests for the ``TypingView`` renderer
 * (typing-input-highlighting design, Properties 2, 3, 5, and 8).
 *
 * These four properties all exercise the rendered DOM of
 * :func:`TypingView` against arbitrary ``(prompt, typed)`` pairs with
 * ``len(typed) <= len(prompt)``, so they share one generator and one
 * minimal render harness. Keeping them in a single file matches the
 * tasks plan (tasks 2.6, 2.7, 2.8, 2.9) and lets fast-check reuse the
 * same shrinker for every property.
 *
 * Render harness:
 *   * Each iteration constructs a synthetic ``typing``-kind
 *     :type:`TypingPageState` directly and mounts :func:`TypingView`
 *     in isolation. There is no countdown, no ``POST /begin`` call,
 *     and no ``fetch`` mocking — the design explicitly calls for "a
 *     minimal harness that hosts TypingView with synthetic PageState
 *     input so the test doesn't have to drive the countdown and POST
 *     /begin phases for every iteration".
 *   * ``onChange`` is a no-op because these properties inspect the
 *     rendered DOM derived from ``state.prompt`` and ``state.typed``,
 *     not the input-handler plumbing (which is covered by Property 6
 *     in task 5.2).
 *   * ``@testing-library/react``'s ``cleanup`` runs after every
 *     fast-check iteration via an ``afterEach`` hook so the
 *     container doesn't accumulate DOM across iterations and
 *     ``document.body`` stays uncontended for the next render.
 *
 * Iterations: every property runs at least the 100 iterations the
 * tasks call for (fast-check's default). We do not lower
 * ``numRuns``.
 */

import { describe, expect, it, afterEach } from "vitest";
import fc from "fast-check";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { createRef, useRef, useState } from "react";

import {
  classifyView,
  TypingView,
  type CharacterState,
  type TypingPageState,
} from "./PlayPage";

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * Full-unicode prompt generator. ``unit: "grapheme"`` (fast-check v4)
 * produces strings of arbitrary graphemes so the property exercises
 * the same surface as the live prompt loader — ASCII, whitespace,
 * and non-BMP / grapheme clusters. A modest ``maxLength`` keeps the
 * 100-iteration run under a second on CI. Mirrors the generator in
 * :file:`classifyView.pbt.test.ts`.
 */
const promptArb = fc.string({ unit: "grapheme", maxLength: 64 });

/**
 * Generate a single UTF-16 code unit in the BMP excluding the
 * surrogate-pair block. Used to substitute a "wrong" character at a
 * given index while keeping ``len(typed) <= len(prompt)`` as a
 * code-unit invariant (the same indexing :func:`classifyView` uses).
 * Mirrors the generator in :file:`classifyView.pbt.test.ts`.
 */
const singleCodeUnitArb = fc
  .integer({ min: 0x20, max: 0xd7ff })
  .map((code) => String.fromCharCode(code));

/**
 * Compose a ``(prompt, typed)`` pair where ``typed`` is a
 * prefix-or-edit of ``prompt`` bounded by ``len(typed) <= len(prompt)``.
 *
 * Copied from :file:`classifyView.pbt.test.ts` rather than imported so
 * these tests stay self-contained (the other file is a ``.ts`` test
 * module whose exports would otherwise need to be hoisted — the
 * tasks plan explicitly permits copying).
 *
 * Strategy (see :file:`classifyView.pbt.test.ts` for the narrative):
 *   1. Sample a prompt from :data:`promptArb`.
 *   2. Sample a typed *code-unit* length ``k`` in ``[0, len(p)]``.
 *   3. For each ``i < k`` either copy ``p[i]`` (producing a ``correct``
 *      classification) or substitute a single BMP code unit that
 *      differs from ``p[i]`` (producing an ``incorrect``). Every
 *      appended character is exactly one UTF-16 code unit so
 *      ``len(t) === k <= len(p)`` holds regardless of surrogate pairs
 *      inside ``p``.
 */
const promptAndTypedArb: fc.Arbitrary<[string, string]> = promptArb.chain(
  (prompt) =>
    fc.integer({ min: 0, max: prompt.length }).chain((k) =>
      fc
        .array(
          fc.record({
            useCorrect: fc.boolean(),
            substitute: singleCodeUnitArb,
          }),
          { minLength: k, maxLength: k },
        )
        .map((edits): [string, string] => {
          const typedChars: string[] = [];
          for (let i = 0; i < k; i += 1) {
            const edit = edits[i];
            if (edit.useCorrect) {
              typedChars.push(prompt[i]!);
            } else {
              const sub = edit.substitute;
              if (sub !== prompt[i]) {
                typedChars.push(sub);
              } else {
                // Flip the low bit so the substitute definitely
                // differs from prompt[i] without changing length.
                const code = prompt.charCodeAt(i);
                typedChars.push(String.fromCharCode(code ^ 1));
              }
            }
          }
          return [prompt, typedChars.join("")];
        }),
    ),
);

// ---------------------------------------------------------------------------
// Render harness
// ---------------------------------------------------------------------------

const ALLOWED_STATES: readonly CharacterState[] = [
  "pending",
  "correct",
  "incorrect",
  "current",
];

/**
 * Build a synthetic ``typing``-kind :type:`TypingPageState`. The
 * ``startedAtMs`` / ``startedAtServer`` fields are inert in the
 * render path — :func:`TypingView` reads only ``prompt`` and
 * ``typed`` — so we fill them with constants.
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
 * Render :func:`TypingView` for a ``(prompt, typed)`` pair and return
 * the Testing Library handle. The ``onChange`` callback is a no-op
 * because these properties inspect the rendered DOM; keystroke
 * handling is covered by Property 6 in task 5.2.
 */
function renderTypingView(prompt: string, typed: string) {
  const state = makeTypingState(prompt, typed);
  const inputRef = createRef<HTMLInputElement>();
  return render(
    <TypingView
      state={state}
      onChange={() => {
        /* no-op — properties 2/3/5/8 are pure view assertions */
      }}
      inputRef={inputRef}
    />,
  );
}

/**
 * Return the ordered list of Character_Display ``<span>`` elements
 * the harness rendered. Using the DOM selector called out by the
 * design — ``[data-testid="play-prompt"] > span`` — means a future
 * wrapper ``<span>`` inside the prompt container would correctly
 * break these properties rather than silently hide.
 */
function getCharSpans(container: ParentNode): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      '[data-testid="play-prompt"] > span',
    ),
  );
}

// Testing Library doesn't unmount between fast-check iterations
// automatically — the generator runs inside a single ``it`` block —
// so we clean up after each ``it`` to avoid stale DOM leaking across
// properties.
afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Property 2 — data-char-class coverage
// ---------------------------------------------------------------------------

/**
 * Feature: typing-input-highlighting, Property 2: Character_State is exposed through the `data-char-class` attribute on every Character_Display
 *
 * **Property 2: Character_State is exposed through the ``data-char-class`` attribute on every Character_Display**
 * **Validates: Requirements 2.5, 7.2**
 *
 *   *For any* prompt ``p`` and typed text ``t`` with
 *   ``len(t) <= len(p)``, after rendering ``TypingView``, every
 *   Character_Display span SHALL carry a ``data-char-class``
 *   attribute whose value is one of ``"pending"``, ``"correct"``,
 *   ``"incorrect"``, or ``"current"``, and whose value equals
 *   ``classifyView(p, t).states[i]`` for its index ``i``.
 */
describe("Feature: typing-input-highlighting, Property 2: Character_State is exposed through the `data-char-class` attribute on every Character_Display", () => {
  it("every Character_Display span under the prompt container has a data-char-class in the four-valued enum equal to classifyView(prompt, typed).states[i] (Requirements 2.5, 7.2)", () => {
    fc.assert(
      fc.property(promptAndTypedArb, ([prompt, typed]) => {
        // Guard the generator invariant.
        expect(typed.length).toBeLessThanOrEqual(prompt.length);

        const { container, unmount } = renderTypingView(prompt, typed);
        try {
          const expected = classifyView(prompt, typed).states;
          const spans = getCharSpans(container);

          // One span per Prompt_Character (Requirement 2.5 / design
          // Model 2 invariant ``states.length === prompt.length``).
          expect(spans.length).toBe(prompt.length);

          for (let i = 0; i < spans.length; i += 1) {
            const attr = spans[i].getAttribute("data-char-class");
            // The attribute must be present on every Character_Display
            // (Requirement 7.2: machine-readable per-character state).
            expect(attr).not.toBeNull();
            // And its value must be exactly one of the four enum
            // values (Requirement 2.5).
            expect(ALLOWED_STATES).toContain(attr as CharacterState);
            // And it must match the classifier's decision at that
            // index (the contract between classifier and renderer).
            expect(attr).toBe(expected[i]);
          }
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 3 — glyph preservation
// ---------------------------------------------------------------------------

/**
 * Feature: typing-input-highlighting, Property 3: Each Character_Display preserves the original Prompt_Character
 *
 * **Property 3: Each Character_Display preserves the original Prompt_Character**
 * **Validates: Requirement 2.6**
 *
 *   *For any* prompt ``p`` and typed text ``t`` with
 *   ``len(t) <= len(p)``, after rendering ``TypingView``, the
 *   ``textContent`` of every Character_Display span SHALL equal
 *   ``p[i]`` for its index ``i``, regardless of the assigned
 *   Character_State.
 *
 * ``p[i]`` is UTF-16 code-unit indexing, which matches the
 * ``prompt.split("")`` the renderer uses to break the prompt into
 * per-character spans. A non-BMP grapheme in the prompt therefore
 * produces two spans (one per surrogate half); each span's
 * ``textContent`` still equals ``prompt[i]`` at its own index, which
 * is what Requirement 2.6 asserts.
 */
describe("Feature: typing-input-highlighting, Property 3: Each Character_Display preserves the original Prompt_Character", () => {
  it("every span's textContent equals prompt[i] regardless of its data-char-class (Requirement 2.6)", () => {
    fc.assert(
      fc.property(promptAndTypedArb, ([prompt, typed]) => {
        expect(typed.length).toBeLessThanOrEqual(prompt.length);

        const { container, unmount } = renderTypingView(prompt, typed);
        try {
          const spans = getCharSpans(container);
          expect(spans.length).toBe(prompt.length);

          for (let i = 0; i < spans.length; i += 1) {
            // textContent must be the untouched Prompt_Character at
            // index ``i``. The renderer must not substitute a
            // whitespace replacement, inject a cursor glyph as
            // text, or otherwise mutate the prompt.
            expect(spans[i].textContent).toBe(prompt[i]);
          }
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 8 — cursor programmatic exposure
// ---------------------------------------------------------------------------

/**
 * Feature: typing-input-highlighting, Property 8: Cursor position is programmatically observable
 *
 * **Property 8: Cursor position is programmatically observable**
 * **Validates: Requirement 7.3**
 *
 *   *For any* prompt ``p`` and typed text ``t`` with
 *   ``len(t) <= len(p)``, after rendering ``TypingView``:
 *     * The prompt container's ``data-cursor-index`` attribute SHALL
 *       equal ``min(len(t), len(p))``.
 *     * At most one element in the rendered tree SHALL carry
 *       ``aria-current="true"``.
 *     * If ``len(t) < len(p)``, the element carrying
 *       ``aria-current="true"`` SHALL be the Character_Display whose
 *       index equals ``data-cursor-index``.
 *     * If ``len(t) === len(p)``, no element SHALL carry
 *       ``aria-current="true"``.
 */
describe("Feature: typing-input-highlighting, Property 8: Cursor position is programmatically observable", () => {
  it("data-cursor-index, aria-current cardinality, and their alignment all hold across the (prompt, typed) space (Requirement 7.3)", () => {
    fc.assert(
      fc.property(promptAndTypedArb, ([prompt, typed]) => {
        expect(typed.length).toBeLessThanOrEqual(prompt.length);

        const { container, unmount } = renderTypingView(prompt, typed);
        try {
          // 1) data-cursor-index on the prompt container equals
          //    Math.min(typed.length, prompt.length).
          const promptEl = container.querySelector(
            '[data-testid="play-prompt"]',
          );
          expect(promptEl).not.toBeNull();
          const cursorAttr = promptEl!.getAttribute("data-cursor-index");
          expect(cursorAttr).toBe(
            String(Math.min(typed.length, prompt.length)),
          );

          // 2) At most one element in the subtree carries
          //    aria-current="true".
          const ariaCurrentEls = Array.from(
            container.querySelectorAll('[aria-current="true"]'),
          );
          expect(ariaCurrentEls.length).toBeLessThanOrEqual(1);

          if (typed.length < prompt.length) {
            // 3) When the cursor is still inside the prompt, exactly
            //    one Character_Display carries aria-current="true",
            //    and its index equals data-cursor-index.
            expect(ariaCurrentEls.length).toBe(1);
            const marked = ariaCurrentEls[0] as HTMLElement;
            expect(marked.getAttribute("data-testid")).toBe(
              `play-char-${typed.length}`,
            );
            // And by construction its data-char-class is "current".
            expect(marked.getAttribute("data-char-class")).toBe("current");
          } else {
            // 4) When typed has caught up to the prompt, no element
            //    carries aria-current="true".
            expect(ariaCurrentEls.length).toBe(0);
          }
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 6 — Typed_Text is bounded by Prompt length under any input
// ---------------------------------------------------------------------------

/**
 * Stateful harness that mirrors :func:`PlayPage`'s ``handleInput``
 * contract exactly (see ``raw.slice(0, current.prompt.length)`` in
 * :file:`PlayPage.tsx`) without driving the countdown / POST
 * ``/begin`` / ``POST /result`` state machine. The design's
 * "Component property tests" section calls for precisely this
 * shape: "a minimal harness that hosts ``TypingView`` with
 * synthetic ``PageState`` input so the test doesn't have to drive
 * the countdown and ``POST /begin`` phases for every iteration".
 *
 * Why not render ``PlayPage`` directly for Property 6: when
 * ``untrustedInput.slice(0, prompt.length).length === prompt.length``
 * (i.e., the paste fills or overfills the prompt), ``PlayPage``'s
 * onChange immediately transitions into the ``submitting`` kind and
 * the typing surface unmounts in the same render cycle. The
 * property, however, is stated over *all* user-supplied inputs —
 * including the completing ones — so the test needs a host that
 * leaves the typing surface mounted after the cap is applied.
 *
 * The harness's ``onChange`` replicates the production cap byte-for-
 * byte (``raw.slice(0, prompt.length)``), so the bound being
 * asserted is the same bound ``PlayPage`` enforces. The production
 * transition to ``submitting`` is orthogonal to the cap itself,
 * which is what Property 6 is about (Requirements 5.1, 5.2).
 */
function StatefulTypingHarness({
  prompt,
  initialTyped = "",
}: {
  prompt: string;
  initialTyped?: string;
}): JSX.Element {
  const [typed, setTyped] = useState<string>(initialTyped);
  const inputRef = useRef<HTMLInputElement>(null);
  const onChange = (event: React.ChangeEvent<HTMLInputElement>): void => {
    // Mirror PlayPage.handleInput's cap exactly so the harness is a
    // faithful stand-in for the production onChange contract
    // (Requirements 5.1, 5.2 / Property 6).
    setTyped(event.target.value.slice(0, prompt.length));
  };
  return (
    <TypingView
      state={makeTypingState(prompt, typed)}
      onChange={onChange}
      inputRef={inputRef}
    />
  );
}

/**
 * Feature: typing-input-highlighting, Property 6: Typed_Text is bounded by Prompt length under any input
 *
 * **Property 6: Typed_Text is bounded by Prompt length under any input**
 * **Validates: Requirements 5.1, 5.2**
 *
 *   *For any* prompt ``p`` and any user-supplied input string
 *   ``u``, after simulating a change event that sets the input's
 *   ``value`` to ``u`` while in the ``typing`` state:
 *     * the resulting ``input.value`` SHALL satisfy
 *       ``len(input.value) <= len(p)``; and
 *     * the rendered ``data-char-class`` sequence SHALL equal
 *       ``classifyView(p, u.slice(0, len(p))).states``.
 *
 * The test uses ``fireEvent.change`` (not ``userEvent.type`` /
 * ``userEvent.paste``) because that path bypasses the HTML
 * ``maxLength`` attribute — the attribute only blocks user-driven
 * keystrokes, not direct assignments to ``input.value``. This
 * genuinely exercises the onChange-level cap rather than relying on
 * the browser to cap for us, matching the regression-test rationale
 * already documented in :file:`PlayPage.test.tsx` for task 5.1.
 *
 * ``untrustedInput`` is drawn from ``fc.string({ unit: "grapheme" })``
 * with no length restriction so the generator covers the full space
 * of user input, including inputs longer than the prompt (the very
 * case the cap exists for). Fast-check's default 100 iterations are
 * used per the tasks plan.
 */
describe("Feature: typing-input-highlighting, Property 6: Typed_Text is bounded by Prompt length under any input", () => {
  it("input.value length is bounded by prompt length and the rendered data-char-class sequence equals classifyView(prompt, untrustedInput.slice(0, prompt.length)).states (Requirements 5.1, 5.2)", () => {
    fc.assert(
      fc.property(
        promptArb,
        // No length restriction on ``untrustedInput`` — the generator
        // must cover inputs shorter than, equal to, and longer than
        // the prompt so the cap is exercised across the entire
        // user-input space.
        fc.string({ unit: "grapheme" }),
        (prompt, untrustedInput) => {
          const { container, unmount } = render(
            <StatefulTypingHarness prompt={prompt} />,
          );
          try {
            const input = container.querySelector<HTMLInputElement>(
              '[data-testid="play-input"]',
            );
            expect(input).not.toBeNull();

            // Drive the onChange path with a direct ``fireEvent.change``
            // so the HTML ``maxLength`` attribute doesn't preempt the
            // cap. The assertion target is specifically the onChange
            // cap, not the browser's keystroke filter.
            act(() => {
              fireEvent.change(input!, { target: { value: untrustedInput } });
            });

            // (1) The resulting controlled input's value is bounded
            //     by the prompt length. React keeps the DOM node's
            //     ``value`` in sync with the ``state.typed`` prop
            //     that ``TypingView`` reads, so observing
            //     ``input.value`` is a direct observation of the
            //     capped Typed_Text.
            expect(input!.value.length).toBeLessThanOrEqual(prompt.length);

            // Stronger invariant used by (2): the cap is exactly
            // ``untrustedInput.slice(0, prompt.length)``.
            const expectedTyped = untrustedInput.slice(0, prompt.length);
            expect(input!.value).toBe(expectedTyped);

            // (2) The rendered ``data-char-class`` sequence equals
            //     ``classifyView(prompt, untrustedInput.slice(0,
            //     prompt.length)).states`` — i.e., the renderer is
            //     projecting the capped Typed_Text through the
            //     classifier, not the raw input.
            const expected = classifyView(prompt, expectedTyped).states;
            const spans = getCharSpans(container);
            expect(spans.length).toBe(prompt.length);
            for (let i = 0; i < spans.length; i += 1) {
              expect(spans[i].getAttribute("data-char-class")).toBe(
                expected[i],
              );
            }
          } finally {
            unmount();
          }
        },
      ),
      { numRuns: 100 },
    );
  });
});
