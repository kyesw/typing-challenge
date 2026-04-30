# Typing Game Backend

FastAPI + SQLite backend for the lounge-style Typing Game. The
dashboard stays current by polling `GET /leaderboard` once per
second; no push channel is used.

## Environment Setup (Conda)

This project uses a dedicated conda environment. To set it up:

```bash
# Create env (one time)
conda create -n typing-game python=3.11 -y

# Activate env
conda activate typing-game

# Install project + dev dependencies (editable)
pip install -e ".[dev]"
```

## Running Tests

```bash
conda activate typing-game
cd backend
pytest
```

## Running the Dev Server

```bash
conda activate typing-game
cd backend
uvicorn app.main:app --reload
```

## Configuration

All runtime configuration lives in `app/config.py` and is driven by environment
variables. See that module for the full list of tunables (session TTL,
`Maximum_Game_Duration`, rate limits, prompt selection policy).

## Layout

- `app/` — FastAPI application package
  - `config.py` — Environment-driven settings
  - `errors.py` — `ApiError` contract and exception handlers
  - `main.py` — FastAPI app factory
- `tests/` — Pytest + Hypothesis test suite
