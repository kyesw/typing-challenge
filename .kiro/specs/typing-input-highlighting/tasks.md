# Implementation Plan: Typing Input Highlighting

## Overview

This plan refactors the typing phase of the existing Typing Game (see `.kiro/specs/typing-game/`) to deliver richer per-character visual feedback. The change is narrowly scoped to `frontend/src/pages/PlayPage.tsx` plus a new pure helper, `classifyView`, that replaces the existing `classifyTyped`. All other Typing_Game concerns — prompt delivery, state machine, countdown, network-loss resilience, scoring, session handling, results, leaderboard — remain governed by the base spec and are out of scope.

Tasks are ordered so the pure classifier lands (and is property-tested) first, followed by the `TypingView` render refactor (per-character states, cursor indicator, typo indicator), then the styling and accessibility hooks, and finally the regression sweep that keeps the existing `PlayPage.test.tsx` suite green. Property-based tests are co-located with the code they validate so each correctness property in `design.md` is exercised close to the change that introduces it.

Convention:
- Sub-tasks postfixed with `*` are optional (typically unit tests, example-level component tests, or extensions to existing suites that supplement the required property-based tests).
- Property-based test sub-tasks covering the nine correctness properties in `design.md` are **required** (no `*`).
- Every property test task references a Property ID from `design.md` and the requirement clauses it validates.
- Code examples use TypeScript (the existing frontend language) and `fast-check` + Vitest (already installed; see `frontend/package.json` and `frontend/vitest.setup.ts`).

## Tasks

- [x] 1. Introduce the `classifyView` pure classifier
  - [x] 1.1 Add the `CharacterState` type and `ClassifyResult` interface
    - In `frontend/src/pages/PlayPage.tsx`, add `export type CharacterState = "pending" | "correct" | "incorrect" | "current";`
    - Add `export interface ClassifyResult { states: readonly CharacterState[]; hasTypo: boolean; cursorIndex: number; }`
    - Keep the existing `CharClass` export only if still required by a transitional caller; remove it once `classifyView` is wired everywhere (task 2.1)
    - _Requirements: 1.7, 2.5, 3.1, 3.3, 4.2, 4.5, 7.2, 7.3_

  - [x] 1.2 Implement `classifyView(prompt, typed): ClassifyResult`
    - Single pass over `[0, prompt.length)` that assigns exactly one `CharacterState` per index per the decision table in `design.md` (Model 1 invariants)
    - Compute `hasTypo` as `states.some(s => s === "incorrect")` in the same pass
    - Compute `cursorIndex = Math.min(typed.length, prompt.length)` and clamp defensively when `typed.length > prompt.length` so the classifier never reads past the prompt
    - Export the function so component and property tests can import it directly
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.1, 3.3, 4.2, 4.5, 5.1, 5.2_

  - [x] 1.3 Write property test for Property 1 (classification decision table)
    - **Property 1: Classification matches the decision table**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.3, 4.4**
    - New file `frontend/src/pages/classifyView.pbt.test.ts`
    - Generate `prompt` via `fc.string({ unit: "grapheme" })` and `typed` as a prefix-or-edit of the prompt bounded by `len(typed) <= len(prompt)`; for every index assert `states[i]` equals exactly the value required by the decision table and is one of the four allowed strings
    - Run at least 100 iterations (fast-check default)

  - [x] 1.4 Write property test for Property 4 (cursor cardinality invariant)
    - **Property 4: Cursor cardinality invariant**
    - **Validates: Requirements 3.1, 3.3**
    - In the same file as task 1.3
    - For `(prompt, typed)` with `len(typed) <= len(prompt)`, assert `states.filter(s => s === "current").length === (len(typed) < len(prompt) ? 1 : 0)`

  - [ ]* 1.5 Write example unit tests for `classifyView`
    - Empty `typed` against any prompt → index 0 is `current`, all others `pending`, `hasTypo === false`, `cursorIndex === 0`
    - `typed === prompt` → all `correct`, no `current`, `hasTypo === false`, `cursorIndex === prompt.length`
    - One mismatched character → exactly one `incorrect`, `hasTypo === true`
    - Defensive: `typed` longer than `prompt` → `states.length === prompt.length`, `cursorIndex === prompt.length`
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 4.2, 4.5, 5.1_

- [x] 2. Refactor `TypingView` to consume `ClassifyResult`
  - [x] 2.1 Wire `TypingView` to `classifyView` and drop `classifyTyped`
    - Replace the existing `useMemo(() => classifyTyped(...), ...)` call with `useMemo(() => classifyView(state.prompt, state.typed), [state.prompt, state.typed])`
    - Destructure `{ states, hasTypo, cursorIndex }` from the memoized result
    - Remove the legacy `classifyTyped` export and its internal usage; update the safe-rendering PBT import (task 4.2) to the new API in the same commit
    - _Requirements: 1.2, 6.1, 6.2_

  - [x] 2.2 Render every `Character_Display` with its four-valued state attribute
    - For each index `i` in the prompt, render `<span key={i} data-testid={`play-char-${i}`} data-char-class={states[i]} className={`char char--${states[i]}`}>{prompt[i]}</span>`
    - Set `aria-current="true"` on (and only on) the span whose `states[i] === "current"`
    - Do not mutate the prompt glyph (no whitespace substitution, no cursor glyph injected as text)
    - _Requirements: 1.1, 2.5, 2.6, 7.1, 7.2, 7.3, 8.1_

  - [x] 2.3 Expose `data-cursor-index` on the prompt container
    - Add `data-cursor-index={cursorIndex}` to the `<p data-testid="play-prompt">` element so assistive technology polling the element tree observes the new Cursor_Position before the next paint
    - _Requirements: 3.2, 7.3_

  - [x] 2.4 Render the aggregate typo indicator conditionally
    - When `hasTypo` is `true`, render `<span role="status" aria-live="polite" data-testid="play-typo-indicator">Typo — backspace to fix</span>`
    - When `hasTypo` is `false`, omit the element entirely from the DOM (do not hide via CSS)
    - Interpolate only constant strings inside the indicator — no Prompt or Typed_Text content
    - _Requirements: 4.1, 4.2, 4.5, 8.2_

  - [x] 2.5 Keep the `onChange` length cap and controlled-input contract
    - Leave the existing `raw.slice(0, current.prompt.length)` cap in place; add an inline comment pointing at Requirements 5.1 / 5.2 and Property 6
    - Preserve `maxLength={state.prompt.length}` on the `<input>` as belt-and-suspenders; server-side the typed-text guard (base task 9.4) is unaffected
    - _Requirements: 5.1, 5.2, 6.1, 6.2_

  - [x] 2.6 Write property test for Property 2 (`data-char-class` coverage)
    - **Property 2: Character_State is exposed through the `data-char-class` attribute on every Character_Display**
    - **Validates: Requirements 2.5, 7.2**
    - New file `frontend/src/pages/TypingView.pbt.test.tsx`
    - Render `TypingView` (or a minimal harness hosting it with a synthetic `typing` `PageState`) for arbitrary `(prompt, typed)` with `len(typed) <= len(prompt)`; assert every `<span>` under `[data-testid="play-prompt"] > span` carries a `data-char-class` in the four-valued enum and equal to `classifyView(prompt, typed).states[i]`
    - Run at least 100 iterations

  - [x] 2.7 Write property test for Property 3 (glyph preservation)
    - **Property 3: Each Character_Display preserves the original Prompt_Character**
    - **Validates: Requirement 2.6**
    - In the same file as task 2.6; for arbitrary `(prompt, typed)` assert every span's `textContent` equals `prompt[i]` regardless of its `data-char-class`

  - [x] 2.8 Write property test for Property 5 (typo indicator biconditional)
    - **Property 5: Aggregate typo indicator is present iff the classification contains an incorrect state**
    - **Validates: Requirements 4.2, 4.5**
    - In the same file as task 2.6; for arbitrary `(prompt, typed)` assert `(container.querySelector('[data-testid="play-typo-indicator"]') !== null) === classifyView(prompt, typed).hasTypo`

  - [x] 2.9 Write property test for Property 8 (cursor programmatic exposure)
    - **Property 8: Cursor position is programmatically observable**
    - **Validates: Requirement 7.3**
    - In the same file as task 2.6; for arbitrary `(prompt, typed)` with `len(typed) <= len(prompt)` assert:
      - the prompt container's `data-cursor-index` equals `Math.min(typed.length, prompt.length)`
      - at most one element in the subtree carries `aria-current="true"`
      - if `typed.length < prompt.length`, the element with `aria-current="true"` has index equal to `data-cursor-index`
      - if `typed.length === prompt.length`, no element carries `aria-current="true"`

- [x] 3. Style the four Character_States and the cursor indicator
  - [x] 3.1 Add CSS rules for `.char--pending`, `.char--correct`, `.char--incorrect`, `.char--current`
    - Create or extend the stylesheet imported by `PlayPage` with four Visually_Distinct treatments, each pairing a color with at least one non-color cue (underline, weight, background, or auxiliary glyph) so state is conveyed without relying on color alone
    - Style `.char--current` with the cursor indicator — a CSS-only treatment (e.g., left-edge border, background block, animated underline) that is visible even when the current character is whitespace
    - Style the aggregate typo indicator (`[data-testid="play-typo-indicator"]`) so the message is perceptible alongside the typing surface without blocking input focus
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 4.2, 7.1_

  - [x] 3.2 Write example component tests for each state's rendered class + attribute
    - New suite in `frontend/src/pages/TypingView.test.tsx` (or extend `PlayPage.test.tsx`)
    - Render the typing surface in four curated states — empty typed, one correct character, one incorrect character, a completed prompt — and assert for the Character_Display at the index of interest:
      - `data-char-class` equals the expected string
      - `className` contains `char--<state>`
      - `aria-current` is `"true"` on exactly the `current` span and absent on the others
    - Four example tests (one per Character_State) rather than a property test, because the contract is a four-valued enum and 100 iterations would repeat the same four assertions
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.3, 7.2, 7.3_

  - [ ]* 3.3 Record a manual accessibility review against WCAG 1.4.1 (Use of Color)
    - Inspect the stylesheet rendered in a browser and confirm each Character_State is distinguishable without color (via decoration, weight, or shape) and that contrast ratios meet WCAG AA for body text
    - This review is not automatable under jsdom and is intentionally captured as an optional task so it does not block the implementation merge; record the outcome in the PR description
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 7.1_

- [x] 4. Preserve safe rendering across the new surface
  - [x] 4.1 Confirm the existing ESLint ban on `dangerouslySetInnerHTML` still applies
    - Re-run the frontend lint step after the refactor; no new uses of `dangerouslySetInnerHTML`, `innerHTML`, or string-based DOM construction are introduced by `TypingView` or the typo indicator
    - _Requirements: 8.1, 8.2_

  - [x] 4.2 Extend the existing safe-rendering PBT to exercise the typo indicator path
    - **Property 9: Safe rendering of highlighted characters and the typo indicator**
    - **Validates: Requirements 8.1, 8.2**
    - Update `frontend/src/__tests__/safe-rendering.pbt.test.tsx` so its `TypedContentHarness` (or a new parallel harness) renders `TypingView` directly — or mirrors its JSX including the aggregate typo indicator — and exercises both `hasTypo === false` and `hasTypo === true`
    - Switch the harness over from `classifyTyped` to `classifyView` as part of task 2.1
    - For arbitrary `(prompt, typed)` drawn from the existing `untrustedText` generator, assert `container.querySelector("script")` is `null` and no element carries an attribute matching `/^on[a-z]+$/i`, in both the no-typo and has-typo branches

- [x] 5. Guard the typing-phase input and verify local-only computation
  - [x] 5.1 Add a regression test for the `onChange` length cap
    - In `PlayPage.test.tsx`, add a test that pastes a string longer than the prompt into the typing input and asserts the rendered `state.typed` length equals `prompt.length` and the rendered Character_State sequence equals `classifyView(prompt, pasted.slice(0, prompt.length)).states`
    - _Requirements: 5.1, 5.2_

  - [x] 5.2 Write property test for Property 6 (input bounded by prompt length)
    - **Property 6: Typed_Text is bounded by Prompt length under any input**
    - **Validates: Requirements 5.1, 5.2**
    - In `frontend/src/pages/TypingView.pbt.test.tsx`, for arbitrary `(prompt, untrustedInput)` render `PlayPage` (or a harness hosting the real `TypingView` and `onChange`), fire a `change` event setting `input.value` to `untrustedInput`, and assert:
      - the resulting `input.value` satisfies `len(value) <= len(prompt)`
      - the rendered `data-char-class` sequence equals `classifyView(prompt, untrustedInput.slice(0, prompt.length)).states`

  - [x] 5.3 Write property test for Property 7 (no network request during typing)
    - **Property 7: No network request is issued during typing**
    - **Validates: Requirements 6.1, 6.2**
    - In a new suite (e.g., `frontend/src/pages/PlayPage.localOnly.pbt.test.tsx`), install a `fetch` mock, mount `PlayPage` through the sessionStorage hand-off path so only `POST /games/{gameId}/begin` is dispatched before typing, and for arbitrary sequences `u_1, …, u_k` of incomplete typed inputs (`len(u_j) < len(prompt)`) fire change events and assert `fetch` was called exactly once across the sequence (the `POST /begin` call)
    - Use `vi.useFakeTimers()` to step past the countdown deterministically

- [x] 6. Checkpoint — ensure the classifier and TypingView refactor pass end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Regression sweep against the base Typing_Game behavior
  - [x] 7.1 Re-run the existing `PlayPage.test.tsx` suite unchanged
    - Confirm every existing test still passes: countdown ticks, prompt fallback to `GET /games/:id`, completion submits `POST /games/:id/result`, timeout UI, abandoned UI, retry path, `classifyTyped` purity (rewritten in terms of `classifyView` if the assertions still need a direct classifier export)
    - Any test that referenced `classifyTyped` directly is updated to import `classifyView` and assert against `ClassifyResult.states`
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x] 7.2 Verify no endpoint contracts or state transitions changed
    - Grep the diff: no edits to `frontend/src/api/client.ts`, to the FastAPI route definitions, or to the page-level `PageState` discriminated union's set of `kind` values
    - Confirm the `POST /games/{gameId}/result` payload shape (`typedText`, `elapsedMs`) and the response handling path are unchanged
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [ ]* 7.3 Spot-check the `ReadyPage → PlayPage → ResultsPage` happy path in a component integration test
    - Optional sanity check that the hand-off and navigation flow still reach `/results/:gameId` with a populated `ResultHandoff` after the refactor; the required guarantee is covered by the existing PlayPage test suite
    - _Requirements: 9.1, 9.2_

- [x] 8. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional; per the design's testing strategy, the following property-based tests are **required** and intentionally not starred: classification decision table (1.3), cursor cardinality (1.4), `data-char-class` coverage (2.6), glyph preservation (2.7), typo indicator biconditional (2.8), cursor programmatic exposure (2.9), safe rendering extension (4.2), input length bounding (5.2), and local-only computation (5.3).
- Each property-based test task references a Property ID from `design.md` and the requirement clauses it validates, matching the convention used by `.kiro/specs/typing-game/tasks.md`.
- The classifier lands before the render refactor so property tests exercise the logic without driving the React state machine, keeping iteration counts high and run times bounded.
- Visual and accessibility review against WCAG color-contrast criteria is a manual step (task 3.3) and is not automatable under jsdom; it is marked optional so the implementation can merge once the machine-verifiable properties hold.
- The existing safe-rendering PBT (`Property 17` in the base spec) is extended rather than duplicated, so the typo-indicator code path is covered without forking the test fixture.

## Workflow Complete

This workflow produced the design and planning artifacts for the Typing Input Highlighting feature:

- `requirements.md` — the feature's acceptance criteria
- `design.md` — architecture, components, data models, correctness properties, and testing strategy
- `tasks.md` — this implementation plan

You can begin executing tasks by opening `tasks.md` and clicking **Start task** next to any task item. Tasks are ordered for incremental progress and each sub-task references the requirements and properties it validates.
