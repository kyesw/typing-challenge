/**
 * Feature: typing-input-highlighting, Property 1: Classification matches the decision table
 *
 * Property-based tests for the pure ``classifyView`` classifier
 * (typing-input-highlighting design, Model 2 / Property 1).
 *
 * **Property 1: Classification matches the decision table**
 * **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.3, 4.4**
 *
 *   *For any* prompt string ``p`` and any typed string ``t`` with
 *   ``len(t) <= len(p)``, and for every index ``i`` in ``[0, len(p))``,
 *   ``classifyView(p, t).states[i]`` SHALL equal:
 *
 *     * ``"current"``   if ``i === len(t)`` (and ``i < len(p)`` by the
 *       loop bound), else
 *     * ``"correct"``   if ``i < len(t)`` and ``t[i] === p[i]``, else
 *     * ``"incorrect"`` if ``i < len(t)`` and ``t[i] !== p[i]``, else
 *     * ``"pending"``   (when ``i > len(t)``).
 *
 *   And ``classifyView(p, t).states[i]`` SHALL be exactly one of the
 *   four allowed strings.
 *
 * Generator strategy:
 *   * Prompts are drawn from ``fc.string({ unit: "grapheme" })`` so the
 *     input space covers ASCII, whitespace, and non-BMP / grapheme
 *     clusters — the three regions where per-character logic is most
 *     likely to break. This is the same full-unicode generator the
 *     existing frontend PBTs (e.g., safe-rendering.pbt.test.tsx) use
 *     under fast-check v4, and the task explicitly calls for it.
 *   * Typed text is generated as a *prefix-or-edit* of the prompt: we
 *     pick a length ``k`` in ``[0, len(p)]`` and, for each index
 *     ``i < k``, either copy ``p[i]`` (producing a ``correct`` at that
 *     index) or substitute a different character (producing an
 *     ``incorrect``). This keeps ``len(t) <= len(p)`` by construction
 *     — the invariant the task requires — while exercising all four
 *     Character_States in the same run.
 *
 * The generator is defined as a single ``fc.Arbitrary<[string, string]>``
 * via ``chain`` so the prompt and typed text are sampled together and
 * fast-check's shrinker can shrink both simultaneously.
 *
 * Iterations: the test runs with fast-check's default of 100 iterations
 * (the task mandates "at least 100"; we don't lower it, and we don't
 * raise it to keep the suite snappy).
 */

import { describe, expect, it } from "vitest";
import fc from "fast-check";

import { classifyView, type CharacterState } from "./PlayPage";

// ---------------------------------------------------------------------------
// Decision table — the single source of truth this property validates
// ---------------------------------------------------------------------------

/**
 * Reference implementation of the Character_State decision table from
 * ``design.md``. Deliberately written as a straight-line ``if`` chain
 * so the test is not just re-executing the production code: a bug
 * that swaps two branches in ``classifyView`` would survive if the
 * expected value were computed by calling ``classifyView`` again, but
 * it will not survive here.
 */
function expectedState(
  prompt: string,
  typed: string,
  i: number,
): CharacterState {
  if (i === typed.length && i < prompt.length) return "current";
  if (i < typed.length && typed[i] === prompt[i]) return "correct";
  if (i < typed.length && typed[i] !== prompt[i]) return "incorrect";
  return "pending";
}

const ALLOWED_STATES: readonly CharacterState[] = [
  "pending",
  "correct",
  "incorrect",
  "current",
];

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * Full-unicode prompt generator. ``unit: "grapheme"`` asks fast-check
 * v4 to emit arbitrary graphemes (including multi-code-unit clusters)
 * so the property exercises the same surface as the live prompt
 * loader. A modest ``maxLength`` keeps iterations fast enough that
 * running the default 100 runs stays well under a second on CI.
 */
const promptArb = fc.string({ unit: "grapheme", maxLength: 64 });

/**
 * Single-code-unit generator used to substitute a "wrong" character
 * at a given position. The decision table in ``classifyView`` indexes
 * into strings by UTF-16 code unit (``prompt[i]`` / ``typed[i]``), so
 * the substitution unit must also be exactly one code unit — otherwise
 * the generated ``typed`` string could exceed the chosen length ``k``
 * in code units and break the ``len(typed) <= len(prompt)`` invariant.
 *
 * fast-check's string generators operate in units of grapheme / code
 * point — neither is guaranteed to be a single UTF-16 code unit (a
 * non-BMP code point is two code units). So we generate a numeric
 * code directly in the BMP range that excludes the surrogate pair
 * block ``[0xD800, 0xDFFF]`` (lone surrogates are ill-formed and
 * ``String.fromCharCode`` over that range would produce one code
 * unit that never equals a paired surrogate inside a non-BMP
 * grapheme in the prompt — which is actually fine for the
 * ``incorrect`` case, but skipping surrogates keeps the generator
 * output human-sensible).
 */
const singleCodeUnitArb = fc
  .integer({ min: 0x20, max: 0xd7ff })
  .map((code) => String.fromCharCode(code));

/**
 * Compose a ``(prompt, typed)`` pair where ``typed`` is a
 * prefix-or-edit of ``prompt`` bounded by ``len(typed) <= len(prompt)``.
 *
 * Strategy:
 *   1. Sample a prompt ``p`` from :data:`promptArb` (full-unicode
 *      graphemes — the prompt is compared code-unit by code-unit by
 *      ``classifyView``, so multi-code-unit clusters inside ``p`` are
 *      valuable coverage).
 *   2. Sample a typed *code-unit* length ``k`` uniformly in
 *      ``[0, len(p)]``.
 *   3. For each position ``i < k`` sample a bit: if set, copy the
 *      code unit ``p[i]`` (which will be classified ``correct``);
 *      otherwise substitute a single BMP code unit chosen so it
 *      differs from ``p[i]`` (hitting the ``incorrect`` branch).
 *   4. Concatenate the ``k`` code units to form ``t``.
 *
 * Because every element appended to ``t`` is exactly one UTF-16 code
 * unit, ``len(t) === k <= len(p)`` by construction, regardless of
 * surrogate pairs or grapheme clusters inside ``p``. This gives us
 * the "prefix-or-edit of the prompt bounded by len(typed) <=
 * len(prompt)" the task calls for, while still exercising prompts
 * that include full-unicode content.
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
              // Copy the code unit at prompt[i] verbatim — this index
              // will be classified ``correct`` by the decision table.
              typedChars.push(prompt[i]!);
            } else {
              // Substitute with a single code unit that differs from
              // prompt[i] so the index is guaranteed to be
              // ``incorrect``. If the random substitute happened to
              // equal prompt[i], flip the low bit of the code unit so
              // the alternative still fits in a single code unit.
              const sub = edit.substitute;
              if (sub !== prompt[i]) {
                typedChars.push(sub);
              } else {
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
// Property
// ---------------------------------------------------------------------------

describe("Feature: typing-input-highlighting, Property 1: Classification matches the decision table", () => {
  it("assigns every index exactly one of the four Character_States per the decision table (Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.3, 4.4)", () => {
    fc.assert(
      fc.property(promptAndTypedArb, ([prompt, typed]) => {
        // Guard the generator invariant: if the generator ever emitted
        // a typed string longer than the prompt the property would be
        // meaningless, so fail loudly rather than silently masking a
        // generator bug.
        expect(typed.length).toBeLessThanOrEqual(prompt.length);

        const { states } = classifyView(prompt, typed);

        // states.length must exactly match the prompt (Requirement 1.1).
        expect(states.length).toBe(prompt.length);

        for (let i = 0; i < prompt.length; i += 1) {
          const actual = states[i];
          // Each state must be one of the four allowed strings
          // (Requirement 1.7).
          expect(ALLOWED_STATES).toContain(actual);
          // And it must exactly match the decision table
          // (Requirements 1.3, 1.4, 1.5, 1.6, 3.2, 4.1, 4.3, 4.4).
          expect(actual).toBe(expectedState(prompt, typed, i));
        }
      }),
      // fast-check's default is 100 runs; the task requires "at least 100".
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 4 — Cursor cardinality invariant
// ---------------------------------------------------------------------------

/**
 * Feature: typing-input-highlighting, Property 4: Cursor cardinality invariant
 *
 * **Property 4: Cursor cardinality invariant**
 * **Validates: Requirements 3.1, 3.3**
 *
 *   *For any* prompt ``p`` and typed text ``t`` with
 *   ``len(t) <= len(p)``, the number of indices ``i`` such that
 *   ``classifyView(p, t).states[i] === "current"`` SHALL equal ``1``
 *   when ``len(t) < len(p)`` and SHALL equal ``0`` when
 *   ``len(t) === len(p)``.
 *
 * This is a direct counting invariant on top of the same
 * ``(prompt, typed)`` space Property 1 exercises, so we reuse
 * :data:`promptAndTypedArb` — which guarantees ``len(t) <= len(p)``
 * by construction — and run the default 100 iterations.
 */
describe("Feature: typing-input-highlighting, Property 4: Cursor cardinality invariant", () => {
  it("renders exactly one 'current' state iff typed is shorter than the prompt (Requirements 3.1, 3.3)", () => {
    fc.assert(
      fc.property(promptAndTypedArb, ([prompt, typed]) => {
        // Guard the generator invariant: Property 4 is only meaningful
        // when ``len(typed) <= len(prompt)``. Fail loudly if the
        // generator ever violates this.
        expect(typed.length).toBeLessThanOrEqual(prompt.length);

        const { states } = classifyView(prompt, typed);

        const currentCount = states.filter((s) => s === "current").length;
        const expected = typed.length < prompt.length ? 1 : 0;

        expect(currentCount).toBe(expected);
      }),
      // fast-check's default is 100 runs; the task requires "at least 100".
      { numRuns: 100 },
    );
  });
});
