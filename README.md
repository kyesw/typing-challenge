# Typing Game

Lounge-style, walk-up typing competition. Python + FastAPI backend with a
React + Vite + TypeScript frontend, shared real-time dashboard over WebSocket.

See `.kiro/specs/typing-game/` for the design, requirements, and task plan.

## Project Layout

```
backend/   FastAPI app, WebSocket channel, SQLite persistence, pytest + Hypothesis
frontend/  Vite + React + TypeScript client, Vitest + Testing Library
```

## Backend — Conda environment

The backend uses a dedicated conda environment named `typing-game` (Python 3.11).

```bash
# One-time setup
conda create -n typing-game python=3.11 -y
conda activate typing-game
cd backend
pip install -e ".[dev]"

# Run tests
pytest
```

See `backend/README.md` for details.

## Frontend

```bash
cd frontend
npm install
npm test -- --run   # Vitest in non-watch mode
npm run build       # type-check + production build
```

See `frontend/README.md` for details.
