# Typing Game Frontend

Vite + React + TypeScript client for the lounge Typing Game.

## Setup

```bash
cd frontend
npm install
```

## Scripts

- `npm run dev` — Vite dev server
- `npm run build` — Type-check and build for production
- `npm test -- --run` — Run the Vitest suite once (no watch)

## Layout

- `src/main.tsx` — App entry + `BrowserRouter`
- `src/App.tsx` — Router with placeholder pages
- `src/pages/` — One component per route
- `src/api/types.ts` — Shared `ApiError` contract mirrored from the backend
