# Implementation Plan: Kiro Theme Frontend

## Overview

Apply a cohesive dark-theme visual design to the Typing Game frontend using a single global CSS stylesheet with CSS custom properties. The implementation proceeds in four phases: (1) create the global `theme.css` with all custom properties, resets, typography, layout, component, and page-specific styles; (2) wire it into the app via `main.tsx`; (3) update `PlayPage.css` color values to dark-palette equivalents; (4) add new-top-player detection logic and crown animation support to `DashboardPage.tsx`. No new dependencies are added.

## Tasks

- [x] 1. Create the global theme stylesheet
  - [x] 1.1 Create `frontend/src/theme.css` with CSS custom properties and global resets
    - Define the `:root` color palette: `--bg-primary`, `--bg-surface`, `--text-primary`, `--text-secondary`, `--accent-blue`, `--accent-teal`, `--accent-green`, `--accent-red`, `--accent-amber`, `--border-default`, `--border-accent`, `--surface-radius`
    - Add CSS reset: `box-sizing: border-box` globally, remove default margins/padding on `body`, set `min-height: 100vh`
    - Style `body` with `--bg-primary` background, `--text-primary` color, system sans-serif font stack, and font smoothing (`-webkit-font-smoothing: antialiased`, `-moz-osx-font-smoothing: grayscale`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 1.2 Add typography, layout, and component styles to `frontend/src/theme.css`
    - Typography: `h1` (≥2rem, weight 700), `h2` (≥1.5rem, weight 600), `p` (`--text-secondary`, line-height ≥1.6), bottom margins on headings
    - Layout: `main` centered with max-width 720px, horizontal padding ≥1.5rem, vertical padding ≥2rem
    - Buttons: gradient pill (`--accent-blue` to `--accent-teal`), `--text-primary` text, border-radius 999px, padding, hover brightness + glow, disabled opacity ~0.5, focus ring with `--accent-blue` outline, transitions ≥150ms
    - Inputs: `--bg-surface` background, `--text-primary` text, `--border-default` border, border-radius 8px, focus glow with `--accent-blue`, padding ≥0.75rem, font-size ≥1rem, transitions ≥150ms
    - Labels: `--text-secondary` color, font-weight 500, bottom margin 0.5rem
    - Alerts: `[role="alert"]` with rgba red background, 3px solid red left border, `--text-primary` text, border-radius 6px, padding ≥0.75rem, fade-in transition ≥200ms
    - Links/Secondary links: `--text-secondary` color, no text-decoration, hover changes to `--accent-blue` with underline, transition ≥150ms, focus indicator
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 13.1, 13.2, 13.3, 13.4_

  - [x] 1.3 Add page-specific styles to `frontend/src/theme.css`
    - Nickname page: form Surface card (`--bg-surface`, `--border-default`, border-radius 12px, padding ≥2rem), vertical stack with ≥1rem gap, centered heading/paragraph
    - Ready page: centered content, conflict dialog Surface (`--bg-surface`, `--border-default`, border-radius 12px, padding ≥1.5rem), Resume/Restart as secondary links
    - Play page: countdown display (`[data-testid="play-countdown"]`) with font-size ≥4rem, `--accent-blue`, weight 700
    - Results page: Stat_Card grid (`dl > div`) with `--bg-surface`, `--border-default`, border-radius 12px, padding ≥1.25rem, 2×2 grid with ≥1rem gap; `dt` styled as label (`--text-secondary`, 0.875rem, uppercase), `dd` styled as value (`--text-primary`, ≥1.75rem); "Play again" as Primary_Button, "View leaderboard" as Secondary_Link
    - Dashboard page: Leaderboard_Table Surface (`--bg-surface`, `--border-default`, border-radius 12px, overflow hidden), `thead` with rgba blue background and `--text-secondary` uppercase text, `tbody` rows with bottom border, hover highlight, cell padding ≥0.75rem, right-aligned numeric columns
    - _Requirements: 7.1, 7.2, 7.3, 8.1, 8.2, 8.3, 9.7, 9.8, 10.1, 10.2, 10.4, 10.5, 10.6, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [x] 1.4 Add responsive rules, animations, and crown animation keyframes to `frontend/src/theme.css`
    - Responsive: `@media (max-width: 768px)` — reduce `h1` to ≥1.5rem, `h2` to ≥1.25rem
    - Responsive: `@media (max-width: 640px)` — reduce `main` padding (horizontal ≥1rem, vertical ≥1.5rem), stack Stat_Cards to single column, reduce table font-size to 0.875rem and cell padding to 0.5rem, ensure 44×44px touch targets
    - Animations: `@keyframes fade-in` on `main` (≥200ms), `prefers-reduced-motion: reduce` override disabling fade-in and non-essential transitions
    - Crown animation: `@keyframes crown-glow` (golden box-shadow pulse ~2s), `.crown-glow` class, `prefers-reduced-motion` fallback with static golden left-border
    - _Requirements: 2.3, 10.3, 11.7, 12.2, 12.5, 14.1, 14.3, 15.1, 15.2, 15.3_

- [x] 2. Wire the theme into the application
  - [x] 2.1 Edit `frontend/src/main.tsx` to import the global theme
    - Add `import './theme.css'` as the first import (before the App import) so the global styles load before any component renders
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 3. Adapt the Play page typing surface to the dark palette
  - [x] 3.1 Edit `frontend/src/pages/PlayPage.css` to update color values
    - Update `[data-testid="play-prompt"]`: background → `var(--bg-surface)`, color → `var(--text-primary)`, border → `1px solid var(--border-default)`
    - Update `.char--pending`: color → `var(--text-secondary)`
    - Update `.char--correct`: color → `var(--accent-green)`
    - Update `.char--incorrect`: color → `var(--accent-red)`, background-color → `rgba(248, 81, 73, 0.15)`
    - Update `.char--current`: color → `var(--accent-blue)`, background-color → `rgba(88, 166, 255, 0.15)`, border-left → `2px solid var(--accent-blue)`
    - Update `@keyframes play-cursor-blink`: border-left-color → `var(--accent-blue)`
    - Update `[data-testid="play-typo-indicator"]`: background-color → `rgba(210, 153, 34, 0.15)`, color → `var(--accent-amber)`, border → `1px solid var(--accent-amber)`
    - Preserve all non-color properties (font-weight, text-decoration, text-underline-offset, border-radius, animation timing, `::before` content)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 14.2_

- [x] 4. Checkpoint — Verify theme renders correctly
  - Build the frontend (`npm run build` in `frontend/`) to confirm no CSS or TypeScript errors. Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement new-top-player detection and crown animation in DashboardPage
  - [x] 5.1 Edit `frontend/src/pages/DashboardPage.tsx` to add crown-glow logic
    - Add a `useRef<string | null>(null)` to track the previous rank-1 `playerId`
    - Add a `useState<string | null>(null)` for `crownPlayerId`
    - When `state.kind` transitions to `"ready"`, compare the new rank-1 `playerId` against the ref; if they differ and the ref is not `null` (not initial load), set `crownPlayerId`
    - Update the ref to the current rank-1 `playerId` after comparison
    - Pass `crownPlayerId` to the `LeaderboardTable` component
    - In `LeaderboardTable`, apply `className="crown-glow"` to the `<tr>` whose `entry.playerId === crownPlayerId`
    - Add an `onAnimationEnd` handler on the crown row to clear `crownPlayerId` back to `null`
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [ ]* 5.2 Write unit tests for new-top-player detection logic
    - Test that no `crown-glow` class appears on initial load
    - Test that `crown-glow` class appears when rank-1 `playerId` changes between snapshots
    - Test that `crown-glow` class does not appear when rank-1 `playerId` stays the same
    - Test that `crown-glow` class is removed after animation completes
    - _Requirements: 12.1, 12.3, 12.4_

- [x] 6. Final checkpoint — Ensure all tests pass
  - Run `npm run build` and `npx vitest --run` in `frontend/`. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- No new dependencies are introduced — the implementation uses plain CSS and existing React hooks
- The theme is a pure visual layer; no existing HTML structure, `data-testid`, `aria-*`, or `role` attributes are modified (Requirement 16.3)
- All color choices maintain WCAG contrast ratios: ≥4.5:1 for primary text, ≥3:1 for secondary text (Requirement 16.1, 16.2)
