# Requirements Document

## Introduction

The Typing Game is a multi-user, lounge-style web application for casual, social typing competition. Players identify themselves with a nickname, play a timed typing challenge against a randomly assigned prompt, and see how they rank on a shared live leaderboard displayed on a lounge dashboard. The experience is walk-up and low-friction: no accounts, just a nickname and a Start button. The client and dashboard are served by a single web application backed by a REST API, with the dashboard polling the leaderboard endpoint once per second to stay current, and a small set of services (Player, Game, Scoring, Prompts) sharing a data store.

This document derives formal requirements from the approved design document and covers every component, flow, data model, error scenario, and non-functional consideration described there.

## Glossary

- **System**: The Typing Game application as a whole (client, dashboard, backend, services, store).
- **Web_Client**: The browser-based player UI that renders the Nickname, Ready, Countdown, Typing, and Results pages.
- **Dashboard_Client**: The read-only lounge display UI rendered at `/dashboard`.
- **Backend_API**: The REST API that coordinates player, game, scoring, and leaderboard actions.
- **Game_Service**: The backend service that owns the lifecycle of a typing game instance.
- **Player_Service**: The backend service that registers and looks up nickname-based player identities.
- **Scoring_Service**: The backend service that computes WPM, accuracy, and points, and updates the leaderboard.
- **Prompt_Repository**: The store that supplies typing prompts to the Game_Service.
- **Data_Store**: The shared persistence layer for players, games, scores, and prompts.
- **Player**: A registered participant identified by a unique nickname for the current lounge session.
- **Nickname**: A display name chosen by a player, 2-20 characters, letters/digits/spaces/hyphens/underscores, unique case-insensitively among active players.
- **Session_Token**: A short-lived token bound to a single nickname used to authorize subsequent requests.
- **Prompt**: A passage of text (100-500 characters) to be typed during a game, with optional difficulty and a language code.
- **Game**: A single typing attempt by a player against a prompt, with status in {`pending`, `in_progress`, `completed`, `abandoned`}.
- **Score**: The computed outcome of a completed game, including `wpm`, `accuracy`, and `points`.
- **WPM**: Words per minute computed by the Scoring_Service from the submitted text and server-measured elapsed time.
- **Accuracy**: Percentage of correctly typed characters in `[0, 100]`.
- **Points**: A composite score derived deterministically from `wpm` and `accuracy`, used for ranking.
- **Leaderboard**: The ranked collection of LeaderboardEntry rows derived from Scores.
- **LeaderboardEntry**: A derived row containing `playerId`, `nickname`, `bestWpm`, `bestAccuracy`, `bestPoints`, and `rank`.
- **Maximum_Game_Duration**: A server-side configured upper bound on the elapsed time of a game's typing phase.
- **Active_Player**: A registered player whose session token has not expired.

## Requirements

### Requirement 1: Nickname Registration

**User Story:** As a walk-up player, I want to register with just a nickname, so that I can start playing without creating an account.

#### Acceptance Criteria

1. WHEN a player opens the application at route `/`, THE Web_Client SHALL display the Nickname entry page.
2. WHEN a player submits a nickname, THE Web_Client SHALL send a `POST /players` request to the Backend_API with the submitted nickname.
3. WHEN the Backend_API receives a valid registration request, THE Player_Service SHALL create a Player record with a unique `id`, the submitted `nickname`, a `createdAt` timestamp, and a newly issued `sessionToken`, and THE Backend_API SHALL return the `playerId` and `sessionToken` to the Web_Client.
4. WHEN the Web_Client receives a successful registration response, THE Web_Client SHALL navigate the player to the Ready page at route `/ready`.
5. IF a submitted nickname is shorter than 2 characters or longer than 20 characters, THEN THE Player_Service SHALL reject the registration with a validation error.
6. IF a submitted nickname contains characters other than letters, digits, spaces, hyphens, or underscores, THEN THE Player_Service SHALL reject the registration with a validation error.
7. IF a submitted nickname matches the nickname of an Active_Player under case-insensitive comparison, THEN THE Player_Service SHALL reject the registration with a duplicate-nickname conflict response.
8. WHEN THE Player_Service rejects a registration due to validation or duplicate nickname, THE Web_Client SHALL display an inline error message and keep the player on the Nickname entry page.

### Requirement 2: Ready and Game Start

**User Story:** As a registered player, I want to start a game from a Ready screen, so that I can begin typing when I am prepared.

#### Acceptance Criteria

1. WHILE a player is on the Ready page, THE Web_Client SHALL display a Start button.
2. WHEN a player clicks the Start button, THE Web_Client SHALL send a `POST /games` request to the Backend_API including the `playerId` and the `Session_Token`.
3. WHEN the Backend_API receives a valid game-start request, THE Game_Service SHALL create a Game record with status `pending`, a newly assigned `promptId` from the Prompt_Repository, and a reference to the `playerId`.
4. WHEN a Game record is created, THE Backend_API SHALL return the `gameId`, the assigned prompt text, and a server-determined `startAt` timestamp to the Web_Client.
5. WHEN the Web_Client receives a successful game-start response, THE Web_Client SHALL navigate the player to route `/play/:gameId` and display the Countdown.
6. IF the player already has a Game with status `in_progress`, THEN THE Game_Service SHALL reject the new game-start request with a conflict response that includes the existing `gameId`.
7. WHEN the Web_Client receives a conflict response that includes an existing `gameId`, THE Web_Client SHALL either route the player into the existing game or offer to abandon and restart.

### Requirement 3: Countdown and Typing Phase

**User Story:** As a player, I want a short countdown and then a responsive typing view, so that I can focus and type without lag.

#### Acceptance Criteria

1. WHEN the Web_Client enters route `/play/:gameId`, THE Web_Client SHALL display a countdown of `3`, `2`, `1` before revealing the typing input.
2. WHEN the countdown ends, THE Web_Client SHALL display the assigned prompt text and a typing input area, and the Game_Service SHALL record the Game's `startedAt` timestamp and transition the Game's status from `pending` to `in_progress`.
3. WHILE the typing phase is active, THE Web_Client SHALL capture keystrokes locally and render real-time visual feedback marking each typed character as correct or incorrect relative to the prompt.
4. WHILE the typing phase is active, THE Web_Client SHALL NOT perform a server round-trip per keystroke for validation.
5. WHEN a player completes the prompt or reaches the end of the typing input, THE Web_Client SHALL send a `POST /games/{gameId}/result` request to the Backend_API with the typed text and the client-reported elapsed time.
6. WHEN the Backend_API receives a result submission, THE Scoring_Service SHALL use a server-measured elapsed time derived from `startedAt` rather than the client-supplied elapsed time to compute the Score.

### Requirement 4: Result Computation and Display

**User Story:** As a player, I want to see my WPM, accuracy, points, and rank after finishing, so that I know how I performed.

#### Acceptance Criteria

1. WHEN the Scoring_Service receives a valid result for a Game in status `in_progress`, THE Scoring_Service SHALL compute `wpm` such that `wpm >= 0`.
2. WHEN the Scoring_Service computes a Score, THE Scoring_Service SHALL compute `accuracy` as a percentage in the closed interval `[0, 100]`.
3. WHEN the Scoring_Service computes a Score, THE Scoring_Service SHALL derive `points` deterministically from `wpm` and `accuracy`.
4. WHEN a Score is computed, THE Scoring_Service SHALL persist exactly one Score record per completed Game, including `gameId`, `playerId`, `wpm`, `accuracy`, `points`, and `createdAt`.
5. WHEN a Score is persisted, THE Game_Service SHALL transition the Game's status from `in_progress` to `completed` and record the Game's `endedAt` timestamp such that `endedAt > startedAt`.
6. WHEN a Score is persisted, THE Backend_API SHALL respond to the result submission with the player's `wpm`, `accuracy`, `points`, and current `rank`.
7. WHEN the Web_Client receives the result response, THE Web_Client SHALL navigate the player to route `/results/:gameId` and display `wpm`, `accuracy`, `points`, and `rank`.

### Requirement 5: Leaderboard Derivation and Ranking

**User Story:** As a lounge observer, I want a ranked leaderboard computed from everyone's scores, so that I can see who is leading.

#### Acceptance Criteria

1. THE Scoring_Service SHALL maintain a Leaderboard derived from persisted Scores, with one LeaderboardEntry per Player who has at least one completed Score.
2. THE Scoring_Service SHALL set each LeaderboardEntry's `bestPoints`, `bestWpm`, and `bestAccuracy` to the maximum corresponding values across that player's Scores.
3. THE Scoring_Service SHALL order LeaderboardEntry rows by `bestPoints` descending, breaking ties by `bestWpm` descending, and breaking remaining ties by earliest Score `createdAt` ascending.
4. THE Scoring_Service SHALL assign each LeaderboardEntry's `rank` starting at 1 for the top entry and increasing by 1 per subsequent entry in the ordered sequence.
5. THE LeaderboardEntry collection SHALL NOT be independently writable and SHALL be recomputed or updated only as a consequence of Score writes.
6. WHEN a client sends `GET /leaderboard`, THE Backend_API SHALL return the current Leaderboard snapshot.

### Requirement 6: Dashboard Live Updates

**User Story:** As a lounge observer, I want the dashboard to refresh automatically as games complete, so that the display feels active and current.

#### Acceptance Criteria

1. WHEN a Dashboard_Client opens route `/dashboard`, THE Dashboard_Client SHALL fetch `GET /leaderboard` for an initial snapshot and render the current top-N Leaderboard rows displaying `nickname`, `bestWpm`, `bestAccuracy`, and `bestPoints`.
2. WHILE the `/dashboard` route is active, THE Dashboard_Client SHALL poll `GET /leaderboard` every 1 second and re-render the displayed top-N rows from each successful snapshot.
3. WHEN a poll request fails, THE Dashboard_Client SHALL continue displaying the last successful snapshot and retry on the next polling tick.

### Requirement 7: Session Token Authorization

**User Story:** As a system operator, I want game actions tied to short-lived session tokens, so that only the owning player can drive their game.

#### Acceptance Criteria

1. THE Player_Service SHALL issue a Session_Token bound to exactly one `playerId` at registration time.
2. THE Backend_API SHALL require a valid Session_Token on `POST /games`, `POST /games/{gameId}/result`, and any other action that modifies a Player's Game state.
3. IF a request to a protected endpoint carries a missing, unknown, or expired Session_Token, THEN THE Backend_API SHALL return an unauthorized response.
4. WHEN the Web_Client receives an unauthorized response from the Backend_API, THE Web_Client SHALL clear its local player state and redirect the player to the Nickname entry page at route `/`.
5. THE Player_Service SHALL enforce a bounded Session_Token lifetime and treat tokens beyond that lifetime as expired.

### Requirement 8: Game State Machine

**User Story:** As a system maintainer, I want game status transitions to follow a defined state machine, so that game records remain consistent.

#### Acceptance Criteria

1. WHEN a Game is created, THE Game_Service SHALL initialize the Game's status to `pending`.
2. WHEN the typing phase begins for a `pending` Game, THE Game_Service SHALL transition the Game's status to `in_progress`.
3. WHEN a Score is persisted for an `in_progress` Game, THE Game_Service SHALL transition the Game's status to `completed`.
4. WHEN a `pending` or `in_progress` Game is abandoned or times out, THE Game_Service SHALL transition the Game's status to `abandoned`.
5. IF a transition is attempted that is not one of `pending → in_progress`, `in_progress → completed`, `pending → abandoned`, or `in_progress → abandoned`, THEN THE Game_Service SHALL reject the transition and leave the Game's status unchanged.
6. THE Game_Service SHALL allow each Player to have at most one Game in status `in_progress` at any time.
7. WHEN a Game has an `endedAt` timestamp, THE Game_Service SHALL ensure `endedAt > startedAt`.

### Requirement 9: Game Timeout Handling

**User Story:** As a player, I want the game to end cleanly if I run past the maximum duration, so that results stay fair.

#### Acceptance Criteria

1. THE Game_Service SHALL enforce a Maximum_Game_Duration on every `in_progress` Game, measured from the server-recorded `startedAt`.
2. IF a `POST /games/{gameId}/result` request arrives after the server-measured elapsed time for the Game exceeds Maximum_Game_Duration, THEN THE Game_Service SHALL transition the Game's status to `abandoned` and THE Backend_API SHALL reject the submission.
3. WHEN the Web_Client receives a submission rejection due to timeout, THE Web_Client SHALL display a "time's up" message and return the player to the Ready page at route `/ready`.
4. IF an `in_progress` Game exceeds Maximum_Game_Duration without any submission, THEN THE Game_Service SHALL transition the Game's status to `abandoned`.

### Requirement 10: Network Loss and Recovery During Typing

**User Story:** As a player, I want my result to submit after a brief network blip, so that a short disconnect does not ruin my attempt.

#### Acceptance Criteria

1. WHILE the typing phase is active, THE Web_Client SHALL continue the local typing timer if the network connection is lost.
2. IF the Web_Client cannot reach the Backend_API at the end of the typing phase, THEN THE Web_Client SHALL buffer the submission locally and retry when connectivity is restored.
3. WHEN connectivity is restored and the buffered submission is accepted, THE Web_Client SHALL display the result as in a normal submission.
4. IF the Game has already been transitioned to `abandoned` by the Game_Service by the time the buffered submission is retried, THEN THE Web_Client SHALL surface an error message and offer the player a new game.

### Requirement 11: Prompt Repository

**User Story:** As a player, I want a varied prompt each game, so that rounds do not feel repetitive.

#### Acceptance Criteria

1. WHEN the Game_Service creates a new Game, THE Prompt_Repository SHALL supply a Prompt using a defined selection policy such as random, difficulty-based, or rotation.
2. THE Prompt_Repository SHALL store each Prompt with a unique `id`, non-empty `text`, and a `language` code.
3. IF a Prompt's `text` is empty or its length is outside the range `[100, 500]` characters, THEN THE Prompt_Repository SHALL reject the Prompt as invalid.
4. WHERE a Prompt has a `difficulty`, THE Prompt_Repository SHALL constrain its value to one of the fixed allowed difficulty values.

### Requirement 12: Game Metadata Retrieval

**User Story:** As a client, I want to fetch a game's prompt and status, so that I can reconnect to an in-progress game or show post-game details.

#### Acceptance Criteria

1. WHEN a client sends `GET /games/{gameId}`, THE Backend_API SHALL return the Game's metadata, including assigned prompt text and current status.
2. IF the requested `gameId` does not exist, THEN THE Backend_API SHALL return a not-found response.

### Requirement 13: Input Sanitization and Output Safety

**User Story:** As a lounge operator, I want nicknames and typed content rendered safely, so that the dashboard cannot be abused through injection or broken layouts.

#### Acceptance Criteria

1. WHEN nickname content is rendered by the Web_Client or Dashboard_Client, THE System SHALL render it in a manner that prevents HTML or script injection.
2. WHEN typed content is echoed to any client surface including the dashboard, THE System SHALL render it in a manner that prevents HTML or script injection.
3. THE Player_Service SHALL reject any nickname that does not satisfy the character-set and length rules defined in Requirement 1.

### Requirement 14: Rate Limiting

**User Story:** As a lounge operator, I want registration and game creation rate-limited, so that the lounge cannot be trivially flooded.

#### Acceptance Criteria

1. THE Backend_API SHALL apply a rate limit to the `POST /players` endpoint per source client.
2. THE Backend_API SHALL apply a rate limit to the `POST /games` endpoint per source client and per Player.
3. IF a request exceeds the configured rate limit, THEN THE Backend_API SHALL return a rate-limit-exceeded response and SHALL NOT perform the requested action.

### Requirement 15: Server-Authoritative Timing

**User Story:** As a system maintainer, I want the server to own game timing, so that scoring cannot be manipulated by the client.

#### Acceptance Criteria

1. THE Game_Service SHALL record `startedAt` and `endedAt` for every Game from server-side clocks.
2. WHEN the Scoring_Service computes `wpm`, THE Scoring_Service SHALL use the elapsed time derived from `endedAt - startedAt` rather than any client-supplied elapsed value.

### Requirement 16: Data Privacy

**User Story:** As a player, I want the system to collect only a nickname, so that playing does not require sharing personal information.

#### Acceptance Criteria

1. THE System SHALL NOT require any personal data beyond a freely chosen nickname in order to register and play.
2. THE Player_Service SHALL NOT persist fields on the Player record beyond those defined in the Player data model.
