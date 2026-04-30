#!/usr/bin/env bash
# Run backend (FastAPI/uvicorn) and frontend (Vite) together for local dev.
#
# Backend runs inside the `typing-game` conda environment.
# Frontend runs with `npm run dev`.
#
# Usage:
#   ./scripts/dev.sh
#
# Env overrides:
#   CONDA_ENV      conda environment name (default: typing-game)
#   BACKEND_HOST   uvicorn host (default: 127.0.0.1)
#   BACKEND_PORT   uvicorn port (default: 8000)
#   FRONTEND_PORT  vite port       (default: 5173)

set -uo pipefail

CONDA_ENV="${CONDA_ENV:-typing-game}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

# --- Locate conda ------------------------------------------------------------
if [[ -z "${CONDA_EXE:-}" ]]; then
  if command -v conda >/dev/null 2>&1; then
    CONDA_EXE="$(command -v conda)"
  else
    echo "error: conda not found on PATH. Install Miniconda/Anaconda or set CONDA_EXE." >&2
    exit 1
  fi
fi
CONDA_BASE="$("$CONDA_EXE" info --base)"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"

# Check env existence without piping conda's stdout (avoids SIGPIPE noise).
env_list="$(conda env list 2>/dev/null)"
if ! awk 'NF && $1 !~ /^#/ {print $1}' <<<"$env_list" | grep -qx "$CONDA_ENV"; then
  echo "error: conda env '$CONDA_ENV' not found. Create it with:" >&2
  echo "  conda create -n $CONDA_ENV python=3.11 -y" >&2
  echo "  conda activate $CONDA_ENV && pip install -e \"$BACKEND_DIR[dev]\"" >&2
  exit 1
fi

# --- Process management ------------------------------------------------------
BACKEND_PID=""
FRONTEND_PID=""
SHUTTING_DOWN=0

kill_proc() {
  local pid="$1" sig="${2:-TERM}"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  # Signal the process group if we can (child is a subshell leader), else the pid.
  kill "-$sig" "-$pid" 2>/dev/null || kill "-$sig" "$pid" 2>/dev/null || true
}

cleanup() {
  local code=$?
  if (( SHUTTING_DOWN )); then return; fi
  SHUTTING_DOWN=1
  trap - INT TERM EXIT
  printf '\n[dev] shutting down...\n'
  kill_proc "$FRONTEND_PID" TERM
  kill_proc "$BACKEND_PID"  TERM
  # Grace period, then force.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    local alive=0
    for pid in "$FRONTEND_PID" "$BACKEND_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && alive=1
    done
    (( alive )) || break
    sleep 0.2
  done
  kill_proc "$FRONTEND_PID" KILL
  kill_proc "$BACKEND_PID"  KILL
  # Swallow bash's own "Terminated: 15" job-table messages from stderr.
  wait 2>/dev/null || true
  exit "$code"
}
trap cleanup INT TERM

# --- Backend -----------------------------------------------------------------
echo "[dev] starting backend (uvicorn) on http://$BACKEND_HOST:$BACKEND_PORT in conda env '$CONDA_ENV'"
(
  conda activate "$CONDA_ENV"
  cd "$BACKEND_DIR"
  exec uvicorn app.main:app --reload --host "$BACKEND_HOST" --port "$BACKEND_PORT"
) &
BACKEND_PID=$!

# --- Frontend ----------------------------------------------------------------
echo "[dev] starting frontend (vite) on http://localhost:$FRONTEND_PORT"
(
  cd "$FRONTEND_DIR"
  exec npm run dev -- --host --port "$FRONTEND_PORT" --strictPort
) &
FRONTEND_PID=$!

echo "[dev] backend  pid=$BACKEND_PID   -> http://$BACKEND_HOST:$BACKEND_PORT  (docs: /docs)"
echo "[dev] frontend pid=$FRONTEND_PID  -> http://localhost:$FRONTEND_PORT"
echo "[dev] press Ctrl+C to stop both"

# Block until either child exits, then run cleanup.
# Loop because `wait -n` can return early on SIGCHLD from already-reaped jobs.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  # `wait -n` with explicit PIDs waits for one of them to exit.
  wait -n "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
done

cleanup
