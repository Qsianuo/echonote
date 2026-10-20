#!/usr/bin/env bash
# EchoNote launcher: version check -> preflight -> deps (cached) -> ready gate.
set -euo pipefail
cd "$(dirname "$0")"

PY_BIN="${PYTHON:-python3}"

if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "[EchoNote] python3 not found. Install Python 3.10 or newer." >&2
  exit 1
fi

if ! "$PY_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[EchoNote] Python 3.10+ is required (3.9 reached end of life in October 2025)." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[EchoNote] Creating virtual environment..."
  "$PY_BIN" -m venv .venv
fi

VENV_PY=.venv/bin/python

echo "[EchoNote] Checking model cache and network..."
"$VENV_PY" tools/preflight.py || {
  echo "[EchoNote] Startup aborted; fix the message above and retry." >&2
  exit 1
}

# Only install when requirements.txt changes, so a second offline start is
# never blocked by pip (PRD FR-043 AC2).
REQ_HASH="$("$VENV_PY" -c 'import hashlib,pathlib;print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest()[:16])')"
MARKER=.venv/.deps-hash
OLD_HASH=""
[ -f "$MARKER" ] && OLD_HASH="$(cat "$MARKER")"

if [ "$REQ_HASH" != "$OLD_HASH" ]; then
  echo "[EchoNote] Installing dependencies..."
  "$VENV_PY" -m pip install -q --upgrade pip
  "$VENV_PY" -m pip install -q -r requirements.txt
  printf '%s\n' "$REQ_HASH" > "$MARKER"
else
  echo "[EchoNote] Dependencies up to date, skipping pip."
fi

HOST="${ECHONOTE_HOST:-127.0.0.1}"
PORT="${ECHONOTE_PORT:-8000}"

echo ""
echo "============================================"
echo "  EchoNote 声记 v0.9.0 tech preview"
echo "  Open: http://$HOST:$PORT (after the model is ready)"
echo "  First run downloads the model; later runs are offline"
echo "============================================"
echo ""

# Open the browser only once /api/ready is true.
"$VENV_PY" tools/wait_ready.py --host "$HOST" --port "$PORT" --open &

exec "$VENV_PY" -m app
