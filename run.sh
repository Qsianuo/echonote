#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[EchoNote] Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[EchoNote] Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo ""
echo "============================================"
echo "  EchoNote is starting"
echo "  Open: http://127.0.0.1:8000"
echo "  The speech model is downloaded on first run"
echo "============================================"
echo ""

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
