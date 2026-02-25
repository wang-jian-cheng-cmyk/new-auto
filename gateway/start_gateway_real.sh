#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"
MODEL="${MODEL:-openai/gpt-5.2}"

cd "$(dirname "$0")"
MODEL="$MODEL" uvicorn main:app --host "$HOST" --port "$PORT" --log-level info --access-log
