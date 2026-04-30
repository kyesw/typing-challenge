# Design Document: Typing Input Highlighting

## Overview

This feature refines the typing phase of the existing Typing Game (see `.kiro/specs/typing-game/`) with richer, real-time per-character visual feedback. As a player types, the Typing_View classifies each character of the prompt against the expected character and renders it in one of four states — `pending`, `correct`, `incorrect`, or `current` — each with a visually distinct, accessibility-friendly treatment. When the player makes a typo, an aggregate typo indicator nudges them to correct it before advancing.

This is a scoped enhancement of the existing frontend. It replaces only the way `frontend/src/pages/PlayPage.tsx` renders the prompt during the `typing` state. All other concerns — prompt delivery, game state, countdown, network resilience, scoring, session handling, results navigation, leaderboard — remain governed by the base Typing Game design and are out of scope.

The two core deliverables are:

1. A pure, deterministic classifier (`classifyView`) that takes the Prompt and current Typed_Text and returns a per-position `Character_State`.
2. A `TypingView` render refactor that consumes that classification, renders one `Character_Display` per Prompt_Character with machine-readable state and an accessibility-friendly cursor indicator, while keeping rendering safe against HTML injection.

The refactor is intentionally narrow: the existing `PlayPage` component keeps its state machine, its network plumbing, and its network-loss resilience path. The changes live inside `TypingView` and in the pure helper that feeds it.

## Architecture

The feature sits entirely in the Web_Client. No backend contract changes, no new endpoints, no new data in transit. The Typing_Game's existing prompt hand-off and `POST /games/{gameId}/result` submission flow remain the source and sink of data.

```mermaid
graph TD
    Ready[ReadyPage<br/>hand-off in sessionStorage] -->|prompt| Play[PlayPage state machine]
    GetGame[GET /games/:id fallback] -->|prompt| Play
    Play -->|prompt + typed| Classify[classifyView]
    Classify -->|Character_State[]| Typing[TypingView render]
    Typing -->|per-char spans + cursor| DOM[DOM]
    Input[text input onChange] -->|Typed_Text| Play
    Play -->|on completion| Submit[POST /games/:id/result]
```

Key architectural choices:

- **Pure classifier, impure renderer.** `classifyView(prompt, typed)` is a pure function. The renderer (`TypingView`) is a thin React component that reads the classifier's output and spreads it across `<span>` elements. Isolating the logic is what lets property-based tests exercise the full state space without driving the React state machine.
- **No new component layer.** The existing `TypingView` helper in `PlayPage.tsx` is extended, not replaced with a new top-level component. This keeps the diff localized to the file the requirements call out.
- **State is still derived on every render.** The current implementation already recomputes the per-character classes from `prompt` and `typed` via `useMemo`. The new design keeps this: there is no new state machine, no typo counter stored in React state, no cursor position stored separately. Derivation on every keystroke is cheap (O(N) over a prompt capped at 500 characters by the base spec's `Prompt` model) and eliminates a class of "state got out of sync with the input" bugs.
- **Safe rendering by construction.** Every Prompt_Character is interpolated as a text child of a React element; there is no `dangerouslySetInnerHTML`, no `innerHTML` assignment, and no attribute is ever computed from prompt content. This is already enforced by the existing ESLint rule banning `dangerouslySetInnerHTML` (task 12.8 in the base tasks); the feature design relies on, rather than loosens, that constraint.

## Components and Interfaces

### Component 1: `classifyView` (pure function)

Purpose: Deterministically compute the per-character classification and the aggregate typo signal from the current prompt and typed text.

Responsibilities:
- For every index `i` in the prompt, return a `Character_State`:
  - `current` if `i` equals `len(typed)` and `i < len(prompt)`
  - `correct` if `i < len(typed)` and `typed[i] === prompt[i]`
  - `incorrect` if `i < len(typed)` and `typed[i] !== prompt[i]`
  - `pending` otherwise
- Return `cursorIndex`, defined as `min(len(typed), len(prompt))`. This is the Cursor_Position value exposed to the renderer (and to assistive technology via the `data-cursor-index` attribute on the prompt container). When the cursor has moved past the end of the prompt, no position carries `current` (see Requirement 3.3) but `cursorIndex` is still available as a scalar for programmatic observation (Requirement 7.3).

Interface:

```ts
type CharacterState = "pending" | "correct" | "incorrect" | "current";

interface ClassifyResult {
  states: readonly CharacterState[];   // one entry per Prompt_Character
  cursorIndex: number;                 // min(len(typed), len(prompt))
}

function classifyView(prompt: string, typed: string): ClassifyResult;
```

This function replaces the existing `classifyTyped` (which returns `readonly CharClass[]` over three states) with a fuller output. The existing `classifyTyped` is repurposed as an internal detail or removed; its tests are subsumed by tests of `classifyView`.

Rationale: returning a single `ClassifyResult` keeps the derivation one pass and one memoization boundary.

### Component 2: `TypingView` (React component)

Purpose: Render the typing surface when the page is in the `typing` state.

Responsibilities:
- Split the Prompt into Prompt_Characters and render one `Character_Display` per character.
- Expose each Character_Display's state as a machine-readable `data-char-class` attribute and a semantic CSS class (`char char--pending` / `char char--correct` / `char char--incorrect` / `char char--current`). The `data-char-class` attribute is the stable contract for tests and assistive tech.
- For the position whose state is `current`, render an additional cursor indicator (see **Cursor indicator** below) and set `aria-current="true"` on that `<span>`.
- Expose `data-cursor-index` on the prompt container so assistive technology polling the element tree observes the new Cursor_Position before the next paint (Requirement 7.3).
- Host the `<input type="text">` that captures Typed_Text. The input is the source of truth for what the player has typed; React's controlled-component contract keeps `input.value` and the page's `state.typed` in lock-step.

Interface (props, unchanged from the existing component except for passing the full `ClassifyResult` instead of `CharClass[]`):

```ts
interface TypingViewProps {
  state: Extract<PageState, { kind: "typing" }>;
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
  inputRef: React.RefObject<HTMLInputElement>;
}
```

Internally:

```ts
const { states, cursorIndex } = useMemo(
  () => classifyView(state.prompt, state.typed),
  [state.prompt, state.typed],
);
```

### Component 3: `Character_Display` (span-level render contract)

Each character cell is a `<span>` with:

- `key={i}` — the Prompt index.
- `data-testid={"play-char-" + i}` — kept from the existing implementation for test compatibility.
- `data-char-class={states[i]}` — the machine-readable Character_State (Requirement 2.5, 7.2).
- `className={"char char--" + states[i]}` — the CSS hook; the four classes each get a distinct visual treatment (Requirement 2.1–2.4) that combines color with at least one non-color cue (underline, weight, or auxiliary glyph, per Requirement 7.1).
- `aria-current="true"` on the `current` span, absent on every other span (Requirement 7.3).
- Text child: the original `Prompt_Character` glyph, nothing else (Requirement 2.6, 8.1).

Whitespace characters receive the same treatment as any other character: the renderer does not map space to a visible glyph because doing so would mutate the Prompt. CSS handles making the cursor position visible on a whitespace character (for example via a left-edge border or a block background on the `current` span).

### Component 4: Cursor indicator

The cursor is rendered as CSS styling on the `Character_Display` whose `data-char-class === "current"`. No separate DOM node is inserted. Rationale:

- It keeps the DOM structure uniform (one `<span>` per prompt character) — simpler to test, cheaper to render.
- It avoids introducing a node whose content is user-facing but whose text is an arbitrary glyph; any visible cursor glyph injected as text would mutate what the test can read from the prompt.
- Assistive technology finds the cursor position via `aria-current="true"` and the `data-cursor-index` attribute on the prompt container, both of which update synchronously with the React state change (before the next paint, Requirement 3.2, 7.3).

### ~~Component 5: Aggregate typo indicator~~ (REMOVED)

The aggregate typo indicator has been removed from the implementation. The `hasTypo` field no longer exists in `ClassifyResult` and no `play-typo-indicator` element is rendered.

## Data Models

This feature introduces one derived, purely in-memory data model; no persistent schema changes.

### Model 1: Character_State (enum)

An enum of four string values driven directly by the requirements glossary.

```ts
type CharacterState = "pending" | "correct" | "incorrect" | "current";
```

Invariants:
- Exactly one of the four values is assigned to each Character_Display (Requirement 1.7).
- `current` appears at most once across the full `states` array (Requirement 3.1).
- `current` appears zero times when `len(typed) >= len(prompt)` (Requirement 3.3).

### Model 2: ClassifyResult (derived)

The output of `classifyView`. Holds everything the renderer needs about a single `(prompt, typed)` snapshot.

```ts
interface ClassifyResult {
  states: readonly CharacterState[];
  cursorIndex: number;
}
```

Invariants:
- `states.length === prompt.length` (Requirement 1.1).
- `cursorIndex === Math.min(typed.length, prompt.length)` (Requirements 3.1, 3.3, 5.1).
- For every index `i`, `states[i]` is determined purely by `(prompt[i], typed[i], i, prompt.length, typed.length)` — the classification at one index never depends on another (no cascading; Requirement 1.3, 1.4).

### Model 3: Typed_Text (bounded string)

Already present in the existing component as `state.typed`. This feature tightens its contract:

- `len(typed) <= len(prompt)` at all times (Requirement 5.1). The existing implementation already caps via `raw.slice(0, current.prompt.length)` in the `onChange` handler; the feature design keeps that cap and adds a component test.
- No character beyond `prompt.length - 1` is ever interpolated into a `Character_Display`, because the renderer iterates over Prompt_Characters, not Typed_Text (Requirement 5.2).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

This feature is a good fit for property-based testing: the core is a pure function (`classifyView`) and the render is a thin, deterministic projection of that function's output. The natural universal quantifier is "for all `(prompt, typed)` inputs in the allowed space." The properties below were derived from the prework analysis and consolidated during property reflection to eliminate redundancy (for example, the per-position decision rules in Requirements 1.3–1.7 all fold into a single classification property, and Requirement 4.4 "replacing an incorrect character flips it to correct" is implied by that classification property's purity).

Property tests use `fast-check` (already installed as a frontend dev dependency, see `frontend/package.json`) with Vitest. String generators use `fc.fullUnicodeString` so the property space covers ASCII, whitespace, and non-BMP code points — the three regions where ad-hoc per-character logic is most likely to break.

### Property 1: Classification matches the decision table

*For any* prompt string `p` and any typed string `t` with `len(t) <= len(p)`, and for every index `i` in `[0, len(p))`, `classifyView(p, t).states[i]` SHALL equal:
- `"current"` if `i === len(t)`, else
- `"correct"` if `i < len(t)` and `t[i] === p[i]`, else
- `"incorrect"` if `i < len(t)` and `t[i] !== p[i]`, else
- `"pending"` (when `i > len(t)`).

And `classifyView(p, t).states[i]` SHALL be exactly one of these four values.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.3, 4.4**

### Property 2: Character_State is exposed through the `data-char-class` attribute on every Character_Display

*For any* prompt `p` and typed text `t` with `len(t) <= len(p)`, after rendering `TypingView`, every Character_Display span SHALL carry a `data-char-class` attribute whose value is one of `"pending"`, `"correct"`, `"incorrect"`, or `"current"`, and whose value equals `classifyView(p, t).states[i]` for its index `i`.

**Validates: Requirements 2.5, 7.2**

### Property 3: Each Character_Display preserves the original Prompt_Character

*For any* prompt `p` and typed text `t` with `len(t) <= len(p)`, after rendering `TypingView`, the `textContent` of every Character_Display span SHALL equal `p[i]` for its index `i`, regardless of the assigned Character_State.

**Validates: Requirement 2.6**

### Property 4: Cursor cardinality invariant

*For any* prompt `p` and typed text `t` with `len(t) <= len(p)`, the number of indices `i` such that `classifyView(p, t).states[i] === "current"` SHALL equal `1` when `len(t) < len(p)` and SHALL equal `0` when `len(t) === len(p)`.

**Validates: Requirements 3.1, 3.3**

### ~~Property 5: Aggregate typo indicator is present iff the classification contains an incorrect state~~ (REMOVED)

This property was removed along with the aggregate typo indicator feature.

### Property 6: Typed_Text is bounded by Prompt length under any input

*For any* prompt `p` and any user-supplied input string `u`, after simulating a change event that sets the input's value to `u` while in the `typing` state, the page's `state.typed` SHALL satisfy `len(state.typed) <= len(p)`, and the rendered Character_State sequence SHALL equal `classifyView(p, u.slice(0, len(p))).states`.

**Validates: Requirements 5.1, 5.2**

### Property 7: No network request is issued during typing

*For any* prompt `p` and any sequence of user-typed inputs `u_1, u_2, …, u_k` that do not complete the prompt (`len(u_j) < len(p)` for every `j`), after mounting `TypingView` with the prompt hand-off path (so the only pre-typing request is `POST /games/{gameId}/begin`) and firing the change events for each `u_j`, the `fetch` mock SHALL have been called exactly once (for `POST /begin`) across the entire sequence.

**Validates: Requirements 6.1, 6.2**

### Property 8: Cursor position is programmatically observable

*For any* prompt `p` and typed text `t` with `len(t) <= len(p)`, after rendering `TypingView`:
- The prompt container's `data-cursor-index` attribute SHALL equal `min(len(t), len(p))`.
- At most one element in the rendered tree SHALL carry `aria-current="true"`.
- If `len(t) < len(p)`, the element carrying `aria-current="true"` SHALL be the Character_Display whose index equals `data-cursor-index`.
- If `len(t) === len(p)`, no element SHALL carry `aria-current="true"`.

**Validates: Requirement 7.3**

### Property 9: Safe rendering of highlighted characters and the typo indicator

*For any* prompt `p` and typed text `t` drawn from `fc.fullUnicodeString`, after rendering `TypingView`, the rendered DOM subtree SHALL contain no `<script>` element and SHALL contain no element carrying an HTML event-handler attribute (any attribute whose name matches `/^on[a-z]+$/i`). This property SHALL be evaluated both when `hasTypo === false` (no aggregate indicator rendered) and when `hasTypo === true` (aggregate indicator rendered) to exercise both code paths.

**Validates: Requirements 8.1, 8.2**

## Error Handling

This feature introduces no new error conditions; every error branch is inherited from the base Typing Game spec. The design's only responsibility is to ensure the highlighting layer does not introduce new failure modes.

### Error Scenario 1: Prompt hand-off is missing

- Condition: The Typing_View mounts without a prompt available in `sessionStorage`.
- Response: Existing `GET /games/{gameId}` fallback runs; the feature is not involved.
- Recovery: Unchanged from the base spec.

### Error Scenario 2: Typed_Text would exceed Prompt length

- Condition: A change event delivers a value longer than `len(prompt)` (e.g., paste, IME, or mobile autocomplete).
- Response: The `onChange` handler truncates the value to `prompt.length`. Rendered Character_States are computed on the truncated value.
- Recovery: No UI change; the player can continue typing or backspace normally. Covered by Property 6.

### Error Scenario 3: `classifyView` receives `len(typed) > len(prompt)`

- Condition: A programming error somewhere upstream passes typed text longer than the prompt into the classifier (should be impossible given the onChange cap but treated defensively).
- Response: The classifier iterates over the prompt indices only. Any excess characters in `typed` are ignored. `cursorIndex` is `min(len(typed), len(prompt))` and is therefore clamped at `len(prompt)`.
- Recovery: No visible UI impact; the render remains consistent with the prompt bounds. This is a guardrail on top of Requirements 5.1 and 5.2.

### Error Scenario 4: Prompt contains unusual Unicode (combining marks, surrogate pairs)

- Condition: Prompt text contains characters that occupy multiple UTF-16 code units or interact in unexpected ways with JavaScript string indexing.
- Response: The classifier indexes the prompt string with the same indexing scheme the input event uses (UTF-16 code units), so `prompt[i]` and `typed[i]` compare consistently. Rendering splits the prompt via `prompt.split("")`, which also splits on UTF-16 code units — the same unit of comparison.
- Recovery: Not a bug per se; it is an acknowledged limitation inherited from JavaScript's string model. The Prompt validator in the base spec constrains prompts to 100–500 characters of printable text, so the risk is low in practice. Safe-rendering (Property 9) still holds because React's text interpolation is UTF-16-agnostic.

## Testing Strategy

### Pure-function unit and property tests (`classifyView`)

- Example unit tests for representative inputs:
  - Empty typed against any prompt → all `pending` except index 0, which is `current`; `hasTypo === false`.
  - Typed equal to prompt → all `correct`; no `current`; `hasTypo === false`.
  - Typed matches prompt character-for-character except one mismatch → exactly one `incorrect`; `hasTypo === true`.
  - Typed longer than prompt (defensive) → `states.length === prompt.length`; excess ignored.
- Property tests (Properties 1, 3, 4) drive the classifier directly with `fc.fullUnicodeString` prompts and a prefix generator for typed text. Each property test is configured to run at least 100 iterations (fast-check's default, made explicit where the feature test has custom configuration).

### Component property tests (`TypingView`)

- Property 2 (state attribute on every span), Property 5 (typo-indicator biconditional), and Property 8 (cursor programmatic exposure) are implemented as Vitest + Testing Library component tests driven by `fast-check`.
- Each test renders a minimal harness that hosts `TypingView` with synthetic `PageState` input so the test doesn't have to drive the countdown and `POST /begin` phases for every iteration.
- Every component property test runs at least 100 iterations.

### Component example tests (visual treatment)

- Four example tests (one per Character_State) verify the rendered CSS class name and `data-char-class` attribute are the expected constants (Requirements 2.1–2.4). These are not property tests because the CSS-class contract is a four-valued enum — 100 iterations would repeat the same four assertions.
- The human-facing question "does the treatment look visually distinct and does it convey state without relying on color alone" (Requirements 2.1–2.4, 7.1) is an accessibility review item, performed by manually inspecting the stylesheet against WCAG 1.4.1 (Use of Color). It is not automatable against jsdom.

### Safe-rendering property test (Property 9, Requirement 8.1/8.2)

- Extends the existing `frontend/src/__tests__/safe-rendering.pbt.test.tsx` to exercise `TypingView` directly, including cases where `hasTypo === true` so the aggregate indicator is in the DOM. Assertions are structural: `container.querySelector("script")` is `null`, and no element carries an attribute whose name matches `/^on[a-z]+$/i`.

### Regression-level component tests (Requirement 9)

- The existing `PlayPage.test.tsx` tests — countdown ticks, prompt fallback, completion submits `POST /games/:id/result`, timeout UI, abandoned UI, retry path — continue to pass unchanged. This is the guarantee that Requirements 9.1–9.4 hold: no change to game flow, state machine, timing rules, or API contract.
- One new component test covers Property 6's event-level surface (paste / IME / oversized input), since that codepath is in the `onChange` handler rather than in `classifyView`.

### Test library and configuration

- Runner: `vitest` (already configured in `frontend/vitest.setup.ts`).
- Property library: `fast-check` (already installed).
- DOM: `jsdom` (already configured).
- Tag format for every property test: `Feature: typing-input-highlighting, Property {number}: {property_text}` in a comment at the top of each test, per the base tasks convention.
- Minimum iterations per property test: 100 (fast-check default is 100; we do not lower it).

### Out of scope for automated testing

- Perceptual distinctness and WCAG color-contrast validation (Requirements 2.1–2.4, 7.1): manual accessibility review against the stylesheet.
- Screen-reader announcement behaviour: not testable under jsdom; exercised via manual testing with an assistive technology as part of the accessibility review.
