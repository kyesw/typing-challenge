import { Route, Routes } from "react-router-dom";
import { NicknamePage } from "./pages/NicknamePage";
import { ReadyPage } from "./pages/ReadyPage";
import { PlayPage } from "./pages/PlayPage";
import { ResultsPage } from "./pages/ResultsPage";
import { DashboardPage } from "./pages/DashboardPage";

/**
 * Top-level router. Each route is a placeholder so navigation and routing
 * are wired up end-to-end; the real page bodies land in later tasks.
 *
 * Routes are defined per design:
 *   /                 – Nickname entry       (Requirement 1.1)
 *   /ready            – Ready / Start        (Requirement 2.1)
 *   /play/:gameId     – Countdown + Typing   (Requirement 3.1)
 *   /results/:gameId  – Player result view   (Requirement 4.7)
 *   /dashboard        – Lounge leaderboard   (Requirement 6.1)
 */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<NicknamePage />} />
      <Route path="/ready" element={<ReadyPage />} />
      <Route path="/play/:gameId" element={<PlayPage />} />
      <Route path="/results/:gameId" element={<ResultsPage />} />
      <Route path="/dashboard" element={<DashboardPage />} />
    </Routes>
  );
}
