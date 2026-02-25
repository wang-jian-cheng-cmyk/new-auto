#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8788}"
HOST="${HOST:-127.0.0.1}"

cd "$(dirname "$0")"
uvicorn main:app --host "$HOST" --port "$PORT" --log-level info --access-log
