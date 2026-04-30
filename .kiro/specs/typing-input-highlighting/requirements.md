# Requirements Document

## Introduction

This feature upgrades the typing phase of the existing Typing Game (see `.kiro/specs/typing-game/`) with richer, real-time per-character visual feedback. As the player types, each character of the prompt is classified against the expected character and rendered with a visually distinct treatment for four states: untyped, correctly typed, incorrectly typed (typo), and the character at the current cursor position. When the player makes a typo, the incorrect character is visibly distinguished from correct characters so the player can see the mistake immediately and correct the input by deleting and retyping before advancing.

This spec is a focused enhancement of the existing Typing Game. It builds on, and does not replace, the base spec's Requirements 3.3 ("render real-time visual feedback marking each typed character as correct or incorrect") and 3.4 (no per-keystroke server round-trip). It adds definition around the visual states, the cursor indicator, typo-correction behavior, accessibility, and rendering safety specific to highlighted characters. Prompt delivery, game state management, scoring, session handling, and all other game mechanics remain governed by the base Typing Game spec and are out of scope here.

## Glossary

- **Typing_Game**: The existing Typing Game application defined by `.kiro/specs/typing-game/`.
- **Web_Client**: The browser-based player UI, as defined by the Typing_Game. This feature modifies only its typing-phase rendering.
- **Typing_View**: The portion of the Web_Client rendered at route `/play/:gameId` during the `typing` phase that displays the prompt and accepts input.
- **Prompt**: The passage of text the player is asked to type, supplied by the Typing_Game server as defined in the base spec.
- **Typed_Text**: The sequence of characters currently held in the Typing_View's input buffer at any instant.
- **Prompt_Character**: A single character at a specific index of the Prompt.
- **Character_Display**: A per-character visual element rendered by the Typing_View for each Prompt_Character.
- **Character_State**: The classification assigned to a Character_Display, one of `pending`, `correct`, `incorrect`, or `current`.
- **Cursor_Position**: The zero-based index into the Prompt equal to the length of the Typed_Text, representing the next character the player is expected to type.
- **Typo**: A Character_Display whose Character_State is `incorrect`.
- **Pending_Index**: A Prompt index that is greater than or equal to the length of Typed_Text.
- **Visually_Distinct**: Rendered with a visual treatment (for example, a different foreground color, background color, underline, or text decoration) that a sighted user can distinguish from every other Character_State's treatment, and that does not rely on color alone.

## Requirements

### Requirement 1: Per-character classification

**User Story:** As a player, I want every character of the prompt to be individually classified as I type, so that I can see at a glance which characters I have typed correctly and which I have mistyped.

#### Acceptance Criteria

1. WHILE the Typing_View is active, THE Typing_View SHALL render one Character_Display for every Prompt_Character in the Prompt.
2. WHEN the Typed_Text changes, THE Typing_View SHALL recompute the Character_State of every Character_Display from the current Typed_Text and the Prompt before the next paint.
3. WHERE a Character_Display's index is less than the length of Typed_Text AND the character at that index in Typed_Text equals the Prompt_Character at that index, THE Typing_View SHALL assign the Character_State `correct` to that Character_Display.
4. WHERE a Character_Display's index is less than the length of Typed_Text AND the character at that index in Typed_Text does not equal the Prompt_Character at that index, THE Typing_View SHALL assign the Character_State `incorrect` to that Character_Display.
5. WHERE a Character_Display's index is a Pending_Index AND that index is not the Cursor_Position, THE Typing_View SHALL assign the Character_State `pending` to that Character_Display.
6. WHERE a Character_Display's index equals the Cursor_Position AND the Cursor_Position is less than the length of the Prompt, THE Typing_View SHALL assign the Character_State `current` to that Character_Display.
7. THE Typing_View SHALL assign exactly one Character_State to each Character_Display.

### Requirement 2: Visual distinction between character states

**User Story:** As a player, I want each character state to look different, so that I can immediately tell correct, incorrect, pending, and current characters apart without reading them.

#### Acceptance Criteria

1. THE Typing_View SHALL render Character_Displays with Character_State `correct` in a Visually_Distinct treatment that differs from `pending`, `incorrect`, and `current`.
2. THE Typing_View SHALL render Character_Displays with Character_State `incorrect` in a Visually_Distinct treatment that differs from `pending`, `correct`, and `current`.
3. THE Typing_View SHALL render Character_Displays with Character_State `pending` in a Visually_Distinct treatment that differs from `correct`, `incorrect`, and `current`.
4. THE Typing_View SHALL render Character_Displays with Character_State `current` in a Visually_Distinct treatment that differs from `correct`, `incorrect`, and `pending`.
5. THE Typing_View SHALL expose each Character_Display's Character_State as a machine-readable attribute on the rendered element so that the state can be asserted in tests and targeted by styling.
6. THE Typing_View SHALL preserve the original Prompt_Character glyph inside every Character_Display regardless of Character_State.

### Requirement 3: Cursor position indicator

**User Story:** As a player, I want to see where I am in the prompt, so that I know which character I need to type next.

#### Acceptance Criteria

1. WHILE the Typing_View is active AND the Cursor_Position is less than the length of the Prompt, THE Typing_View SHALL render exactly one Character_Display with Character_State `current`.
2. WHEN the Cursor_Position changes as a result of a keystroke, THE Typing_View SHALL update which Character_Display has Character_State `current` before the next paint.
3. WHILE the Cursor_Position equals the length of the Prompt, THE Typing_View SHALL render zero Character_Displays with Character_State `current`.

### Requirement 4: Typo feedback and correction prompting

**User Story:** As a player, I want incorrect characters to appear differently from correct ones so that I notice my typos and know to correct them.

#### Acceptance Criteria

1. WHEN the Typed_Text introduces a new character at an index whose value does not equal the Prompt_Character at that index, THE Typing_View SHALL render the Character_Display at that index with Character_State `incorrect` before the next paint.
2. ~~REMOVED~~ *(Aggregate typo indicator was removed.)*
3. WHEN the player removes characters from the Typed_Text such that an index previously classified `incorrect` becomes a Pending_Index, THE Typing_View SHALL reassign that Character_Display's Character_State to `pending` or `current` according to Requirement 1 before the next paint.
4. WHEN the Typed_Text at an index previously classified `incorrect` is replaced such that the new character equals the Prompt_Character at that index, THE Typing_View SHALL reassign that Character_Display's Character_State to `correct` before the next paint.
5. ~~REMOVED~~ *(Aggregate typo indicator was removed.)*

### Requirement 5: Input length bounding

**User Story:** As a player, I want the typing input to stop accepting extra characters past the end of the prompt, so that my typing stays aligned with the highlighted display.

#### Acceptance Criteria

1. WHILE the Typing_View is active, THE Typing_View SHALL bound the length of Typed_Text to be less than or equal to the length of the Prompt.
2. IF a keystroke would extend the Typed_Text beyond the length of the Prompt, THEN THE Typing_View SHALL discard the extra input and leave the Character_State assignments unchanged for that keystroke.

### Requirement 6: Local-only classification

**User Story:** As a player, I want typing feedback to appear instantly as I type, so that the experience feels responsive.

#### Acceptance Criteria

1. WHILE the Typing_View is active, THE Typing_View SHALL compute every Character_State locally on the client without issuing a network request.
2. THE Typing_View SHALL reuse the existing server-supplied Prompt without refetching it per keystroke.

### Requirement 7: Accessibility of character state feedback

**User Story:** As a player with low vision or color blindness, I want character state feedback that does not rely on color alone, so that I can play the game using any color scheme or assistive technology.

#### Acceptance Criteria

1. THE Typing_View SHALL convey each Character_State through at least one non-color cue such as text decoration, weight, shape, or auxiliary glyph, in addition to any color used.
2. THE Typing_View SHALL expose each Character_Display's Character_State through a programmatic attribute that assistive technologies can query.
3. WHEN the Character_State of the Character_Display at the Cursor_Position changes, THE Typing_View SHALL update the programmatic representation of the Cursor_Position such that an assistive technology polling the element tree observes the new Cursor_Position before the next paint.

### Requirement 8: Safe rendering of highlighted characters

**User Story:** As a lounge operator, I want highlighted prompt characters rendered safely, so that prompt content cannot be interpreted as executable markup.

#### Acceptance Criteria

1. WHEN a Character_Display renders a Prompt_Character, THE Typing_View SHALL interpolate the character as text and SHALL NOT interpret the character as HTML or script.
2. ~~REMOVED~~ *(Aggregate typo indicator was removed.)*

### Requirement 9: Preservation of base Typing_Game behavior

**User Story:** As a player, I want the upgraded typing feedback to work inside the existing Typing_Game flow, so that nothing about prompt delivery, game state, timing, or scoring changes.

#### Acceptance Criteria

1. THE Typing_View SHALL receive the Prompt through the same hand-off and fallback mechanism defined by the base Typing_Game spec.
2. THE Typing_View SHALL continue to submit the final Typed_Text through the same `POST /games/{gameId}/result` flow defined by the base Typing_Game spec when the Cursor_Position reaches the length of the Prompt.
3. THE Typing_View SHALL NOT change the set of Game states, transitions, or timing rules defined by the base Typing_Game spec.
4. THE Typing_View SHALL NOT introduce any new server endpoint or modify the existing endpoint contracts of the base Typing_Game spec.
