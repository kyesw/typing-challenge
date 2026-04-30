/// <reference types="vitest" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev-only proxy: when you run ``npm run dev`` the Vite server binds
// to port 5173 and has no routes of its own. The frontend's
// ``apiFetch`` defaults to same-origin paths like ``/players``,
// ``/games``, ``/leaderboard``, and ``/games/{id}/result``, so
// without a proxy every API call 404s against Vite itself.
//
// Forwarding those paths to the FastAPI backend (default port 8000)
// sidesteps CORS entirely — the browser sees everything as
// same-origin against the dev server. In production the frontend
// is served from the same origin as the API, so no proxy is needed
// there.
//
// Override the backend URL per-shell with ``VITE_DEV_API_TARGET``
// (e.g., ``VITE_DEV_API_TARGET=http://localhost:9000 npm run dev``).
// ``loadEnv`` reads ``.env`` files and the process environment in a
// way that works during both ``tsc -b`` and ``vite`` invocations —
// avoiding a hard dependency on ``@types/node`` for ``process.env``.

// Backend route prefixes the dev server should forward. Keep this
// list narrow so unrelated dev paths (e.g., Vite's own HMR endpoint
// at ``/@vite``) stay local.
const PROXY_PATHS = ["/players", "/games", "/leaderboard", "/health"];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const devApiTarget = env.VITE_DEV_API_TARGET ?? "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      proxy: Object.fromEntries(
        PROXY_PATHS.map((path) => [
          path,
          {
            target: devApiTarget,
            changeOrigin: true,
          },
        ]),
      ),
    },
    test: {
      globals: true,
      environment: "jsdom",
      setupFiles: ["./vitest.setup.ts"],
      css: false,
    },
  };
});
