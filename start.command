#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source ".venv/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

( sleep 1; open "http://127.0.0.1:8000" ) &
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
