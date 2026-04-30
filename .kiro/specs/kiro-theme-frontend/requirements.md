# Requirements Document

## Introduction

Apply a cohesive dark-theme visual design inspired by the kiro.dev website to every page of the Typing Game frontend. The theme covers global foundations (color palette, typography, layout), per-page component styling (forms, buttons, tables, cards, stat readouts), responsive behaviour across viewport sizes, and smooth transitions and animations. The existing character-state visual treatments on the typing surface are preserved and adapted to the dark palette rather than replaced.

## Glossary

- **Theme**: The complete set of CSS custom properties, global resets, and component styles that define the visual appearance of the Typing Game frontend.
- **Color_Palette**: The collection of CSS custom properties defining background, surface, text, accent, border, and state colours derived from the kiro.dev design language.
- **Surface**: A visually distinct container (card, panel, or section) rendered on top of the page background, typically with a slightly lighter background, rounded corners, and a subtle border.
- **Typing_Surface**: The monospace prompt container on the Play page that renders per-character feedback using the four Character_State classes (`char--pending`, `char--correct`, `char--incorrect`, `char--current`).
- **Stat_Card**: A display element on the Results page that presents a single metric (WPM, Accuracy, Points, or Rank) inside a styled Surface.
- **Leaderboard_Table**: The `<table>` element on the Dashboard page that renders the top-N player scores.
- **Primary_Button**: A pill-shaped button used for the main call-to-action on each page (Start, Play again, Submit).
- **Secondary_Link**: A styled anchor or link used for non-primary navigation actions (View leaderboard, Back to Ready).
- **Transition**: A CSS transition or animation applied to interactive elements to provide smooth visual feedback on state changes.
- **Breakpoint_Small**: A CSS media query threshold at 640px viewport width, below which the layout adapts for mobile devices.
- **Breakpoint_Medium**: A CSS media query threshold at 768px viewport width, used for tablet-sized viewports.
- **New_Top_Player**: A condition detected on the Dashboard when a leaderboard poll returns a rank-1 `playerId` that differs from the previous snapshot's rank-1 `playerId`. The initial snapshot load does not count as a change.
- **Crown_Animation**: A brief, eye-catching CSS animation played on the rank-1 row of the Leaderboard_Table when a New_Top_Player is detected.

## Requirements

### Requirement 1: Global Dark Theme Foundation

**User Story:** As a player, I want the typing game to have a polished dark theme so that the interface feels modern and is comfortable to use in low-light environments.

#### Acceptance Criteria

1. THE Theme SHALL define a Color_Palette using CSS custom properties on the `:root` selector, including at minimum: `--bg-primary` (deep navy/charcoal, approximately #0d1117), `--bg-surface` (slightly lighter surface, approximately #161b22), `--text-primary` (light text, approximately #e6edf3), `--text-secondary` (muted text, approximately #8b949e), `--accent-blue` (vibrant blue, approximately #58a6ff), `--accent-teal` (teal highlight, approximately #3fb9a0), `--border-default` (subtle border, approximately #30363d), and `--border-accent` (accent border, approximately #58a6ff).
2. THE Theme SHALL apply `--bg-primary` as the `background-color` and `--text-primary` as the `color` on the `<body>` element.
3. THE Theme SHALL set a system sans-serif font stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, "Fira Sans", "Droid Sans", "Helvetica Neue", sans-serif`) on the `<body>` element.
4. THE Theme SHALL apply a CSS reset that removes default margins and padding from `body`, sets `box-sizing: border-box` globally, and ensures `min-height: 100vh` on the `<body>` element.
5. THE Theme SHALL set smooth font rendering using `-webkit-font-smoothing: antialiased` and `-moz-osx-font-smoothing: grayscale` on the `<body>` element.

### Requirement 2: Layout and Container System

**User Story:** As a player, I want the game content to be centered and well-spaced so that the interface is easy to read and navigate on any screen size.

#### Acceptance Criteria

1. THE Theme SHALL center each page's `<main>` element horizontally with a maximum width of 720px and horizontal padding of at least 1.5rem.
2. THE Theme SHALL apply vertical padding of at least 2rem to each page's `<main>` element so content does not touch the viewport edges.
3. WHEN the viewport width is below Breakpoint_Small, THE Theme SHALL reduce the horizontal padding on `<main>` to at least 1rem and the vertical padding to at least 1.5rem.

### Requirement 3: Typography Hierarchy

**User Story:** As a player, I want clear visual hierarchy in headings and body text so that I can quickly scan each page.

#### Acceptance Criteria

1. THE Theme SHALL style `<h1>` elements with a font size of at least 2rem, a font weight of 700, and `--text-primary` colour.
2. THE Theme SHALL style `<h2>` elements with a font size of at least 1.5rem, a font weight of 600, and `--text-primary` colour.
3. THE Theme SHALL style `<p>` elements with `--text-secondary` colour and a line height of at least 1.6.
4. THE Theme SHALL apply a bottom margin of at least 0.5rem to `<h1>` and `<h2>` elements to separate them from subsequent content.

### Requirement 4: Primary Button Styling

**User Story:** As a player, I want buttons to look inviting and clearly clickable so that I know where to take action.

#### Acceptance Criteria

1. THE Theme SHALL style each Primary_Button with a background gradient from `--accent-blue` to `--accent-teal`, `--text-primary` text colour, no visible border, border-radius of 999px (pill shape), horizontal padding of at least 1.5rem, and vertical padding of at least 0.75rem.
2. WHEN a player hovers over a Primary_Button, THE Theme SHALL increase the button brightness using a CSS filter or opacity change and apply a subtle box-shadow glow using `--accent-blue` at reduced opacity.
3. WHEN a Primary_Button is in the disabled state, THE Theme SHALL reduce the button opacity to approximately 0.5 and set `cursor: not-allowed`.
4. THE Theme SHALL apply a Transition of at least 150ms to Primary_Button background, box-shadow, and opacity properties.
5. WHEN a Primary_Button receives keyboard focus, THE Theme SHALL display a visible focus ring using a 2px outline offset from the button edge in `--accent-blue` colour.

### Requirement 5: Form Input Styling

**User Story:** As a player, I want the nickname input field to match the dark theme so that the entry form feels integrated with the rest of the interface.

#### Acceptance Criteria

1. THE Theme SHALL style text input fields with `--bg-surface` background, `--text-primary` text colour, a 1px solid `--border-default` border, and border-radius of 8px.
2. WHEN a text input field receives focus, THE Theme SHALL change the border colour to `--accent-blue` and apply a subtle box-shadow glow using `--accent-blue` at reduced opacity.
3. THE Theme SHALL apply padding of at least 0.75rem inside text input fields and set the font size to at least 1rem.
4. THE Theme SHALL style `<label>` elements with `--text-secondary` colour, font-weight of 500, and a bottom margin of 0.5rem.
5. THE Theme SHALL apply a Transition of at least 150ms to input border-color and box-shadow properties.

### Requirement 6: Error Message Styling

**User Story:** As a player, I want error messages to be clearly visible against the dark background so that I can understand what went wrong.

#### Acceptance Criteria

1. THE Theme SHALL style elements with `role="alert"` with a background colour of approximately rgba(248, 81, 73, 0.1), a left border of 3px solid in a red accent colour (approximately #f85149), `--text-primary` text colour, border-radius of 6px, and padding of at least 0.75rem.
2. THE Theme SHALL apply a Transition of at least 200ms to the opacity of error message elements so they fade in when they appear.

### Requirement 7: Nickname Page Styling

**User Story:** As a player, I want the nickname entry page to feel welcoming and guide me toward entering my name.

#### Acceptance Criteria

1. THE Theme SHALL render the nickname form inside a Surface with `--bg-surface` background, a 1px solid `--border-default` border, border-radius of 12px, and padding of at least 2rem.
2. THE Theme SHALL display the form elements (label, input, button) in a vertical stack with at least 1rem gap between each element.
3. THE Theme SHALL center the page heading and introductory paragraph text above the form Surface.

### Requirement 8: Ready Page Styling

**User Story:** As a player, I want the ready page to build anticipation with a prominent start button.

#### Acceptance Criteria

1. THE Theme SHALL center the heading, instruction text, and Start Primary_Button vertically and horizontally within the `<main>` container.
2. THE Theme SHALL render the conflict alert dialog (`[role="alertdialog"]`) inside a Surface with `--bg-surface` background, a 1px solid `--border-default` border, border-radius of 12px, and padding of at least 1.5rem.
3. THE Theme SHALL style the Resume and Restart buttons inside the conflict dialog as Secondary_Links with `--text-secondary` colour that changes to `--accent-blue` on hover.

### Requirement 9: Play Page and Typing Surface Styling

**User Story:** As a player, I want the typing surface to fit the dark theme while keeping the character-state feedback clear and readable.

#### Acceptance Criteria

1. THE Theme SHALL update the Typing_Surface container (`[data-testid="play-prompt"]`) to use `--bg-surface` background, `--text-primary` base text colour, a 1px solid `--border-default` border, and border-radius of 8px.
2. THE Theme SHALL update the `char--pending` class to use `--text-secondary` colour.
3. THE Theme SHALL update the `char--correct` class to use a green accent colour (approximately #3fb950) with bold weight and a solid underline.
4. THE Theme SHALL update the `char--incorrect` class to use a red accent colour (approximately #f85149) with bold weight, a wavy underline, and a background of approximately rgba(248, 81, 73, 0.15).
5. THE Theme SHALL update the `char--current` class to use `--accent-blue` colour, a left border in `--accent-blue`, and a background of approximately rgba(88, 166, 255, 0.15).
6. THE Theme SHALL update the typo indicator (`[data-testid="play-typo-indicator"]`) to use a dark-theme-appropriate warning style with an amber/yellow accent colour (approximately #d29922) on a dark background (approximately rgba(210, 153, 34, 0.15)).
7. THE Theme SHALL style the typing `<input>` element to match the form input styling defined in Requirement 5.
8. THE Theme SHALL style the countdown display (`[data-testid="play-countdown"]`) with a font size of at least 4rem, `--accent-blue` colour, and font-weight of 700.

### Requirement 10: Results Page Styling

**User Story:** As a player, I want my results displayed in an attractive card layout so that my performance feels rewarding to review.

#### Acceptance Criteria

1. THE Theme SHALL render each stat (WPM, Accuracy, Points, Rank) inside a Stat_Card with `--bg-surface` background, a 1px solid `--border-default` border, border-radius of 12px, and padding of at least 1.25rem.
2. THE Theme SHALL arrange the four Stat_Cards in a 2×2 grid layout with a gap of at least 1rem between cards.
3. WHEN the viewport width is below Breakpoint_Small, THE Theme SHALL stack the Stat_Cards in a single column.
4. THE Theme SHALL style the `<dt>` label inside each Stat_Card with `--text-secondary` colour, font-size of 0.875rem, and text-transform uppercase.
5. THE Theme SHALL style the `<dd>` value inside each Stat_Card with `--text-primary` colour and font-size of at least 1.75rem.
6. THE Theme SHALL style the "Play again" action as a Primary_Button and the "View leaderboard" action as a Secondary_Link.

### Requirement 11: Dashboard and Leaderboard Styling

**User Story:** As a player, I want the leaderboard to look polished and lounge-ready so that checking scores feels like a premium experience.

#### Acceptance Criteria

1. THE Theme SHALL render the Leaderboard_Table inside a Surface with `--bg-surface` background, a 1px solid `--border-default` border, border-radius of 12px, and overflow hidden to clip the table corners.
2. THE Theme SHALL style the `<thead>` row with a background of approximately rgba(88, 166, 255, 0.08) and `--text-secondary` text colour with text-transform uppercase and font-size of 0.8rem.
3. THE Theme SHALL style each `<tbody>` row with a bottom border of 1px solid `--border-default`.
4. WHEN a player hovers over a `<tbody>` row, THE Theme SHALL change the row background to approximately rgba(88, 166, 255, 0.04).
5. THE Theme SHALL apply padding of at least 0.75rem to each `<th>` and `<td>` cell.
6. THE Theme SHALL right-align the WPM, Accuracy, and Points columns using `text-align: right` for numeric readability.
7. WHEN the viewport width is below Breakpoint_Small, THE Theme SHALL reduce the table font-size to 0.875rem and cell padding to 0.5rem so the table fits without horizontal scrolling.

### Requirement 12: New Top Player Celebration Animation

**User Story:** As a lounge observer watching the dashboard, I want a celebration animation when a new player takes the #1 spot, so that the moment feels exciting and the leaderboard feels alive.

#### Acceptance Criteria

1. WHEN the Dashboard_Client detects a New_Top_Player (the rank-1 `playerId` in the current snapshot differs from the rank-1 `playerId` in the previous snapshot), THE Dashboard_Client SHALL apply the Crown_Animation to the rank-1 row of the Leaderboard_Table.
2. THE Crown_Animation SHALL consist of a golden glow effect (box-shadow or background pulse using an accent-gold colour approximately #f0c040) that plays for approximately 2 seconds and then fades out, leaving the row in its normal styled state.
3. THE Dashboard_Client SHALL NOT trigger the Crown_Animation on the initial leaderboard load (the first snapshot has no previous snapshot to compare against).
4. THE Dashboard_Client SHALL NOT trigger the Crown_Animation when the rank-1 player remains the same between consecutive snapshots, even if their score values change.
5. WHILE the `prefers-reduced-motion: reduce` media query is active, THE Crown_Animation SHALL be replaced with a static golden left-border highlight on the rank-1 row that fades out over 2 seconds, instead of the animated glow.

### Requirement 13: Secondary Link Styling

**User Story:** As a player, I want secondary navigation links to be visible but not compete with the primary action button.

#### Acceptance Criteria

1. THE Theme SHALL style each Secondary_Link with `--text-secondary` colour, no text-decoration, and font-size matching the body text.
2. WHEN a player hovers over a Secondary_Link, THE Theme SHALL change the colour to `--accent-blue` and add an underline text-decoration.
3. THE Theme SHALL apply a Transition of at least 150ms to Secondary_Link colour and text-decoration properties.
4. WHEN a Secondary_Link receives keyboard focus, THE Theme SHALL display a visible focus indicator using `--accent-blue` colour.

### Requirement 14: Transitions and Animations

**User Story:** As a player, I want smooth visual transitions so that the interface feels responsive and polished.

#### Acceptance Criteria

1. THE Theme SHALL apply a fade-in Transition of at least 200ms to each page's `<main>` element when it mounts.
2. THE Theme SHALL preserve the existing cursor blink animation on `char--current` and adapt its colour to `--accent-blue`.
3. WHILE the `prefers-reduced-motion: reduce` media query is active, THE Theme SHALL disable all fade-in animations and transitions except essential state indicators (focus rings and the cursor blink fallback).

### Requirement 15: Responsive Design

**User Story:** As a player, I want the game to look good on my phone, tablet, and desktop so that I can play from any device.

#### Acceptance Criteria

1. WHEN the viewport width is below Breakpoint_Medium, THE Theme SHALL reduce `<h1>` font-size to at least 1.5rem and `<h2>` font-size to at least 1.25rem.
2. WHEN the viewport width is below Breakpoint_Small, THE Theme SHALL ensure all interactive elements (buttons, inputs) have a minimum touch target size of 44×44px.
3. THE Theme SHALL ensure no horizontal overflow or scrollbar appears on any page at viewport widths down to 320px.

### Requirement 16: Accessibility Preservation

**User Story:** As a player using assistive technology, I want the theme to maintain all existing accessibility features so that the game remains usable for everyone.

#### Acceptance Criteria

1. THE Theme SHALL maintain a minimum contrast ratio of 4.5:1 between `--text-primary` and `--bg-primary`, and between `--text-primary` and `--bg-surface`.
2. THE Theme SHALL maintain a minimum contrast ratio of 3:1 between `--text-secondary` and `--bg-primary`, and between `--text-secondary` and `--bg-surface`.
3. THE Theme SHALL preserve all existing `aria-*` attributes, `role` attributes, and `data-testid` attributes without modification.
4. THE Theme SHALL ensure every focusable element has a visible focus indicator that meets WCAG 2.1 Level AA requirements.
