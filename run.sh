#!/usr/bin/env bash
# Linux/macOS launcher: creates a venv, installs deps, runs the bot (restarts it if it crashes).
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Put your DISCORD_TOKEN in .env, then run ./run.sh again."
  exit 0
fi
while true; do
  rc=0; python -m bot.main || rc=$?
  if [ "$rc" -eq 78 ]; then echo "[run] Fix the configuration above, then run again."; exit 1; fi
  echo "[run] Bot stopped. Restarting in 10s (Ctrl+C to quit)..."
  sleep 10
done
