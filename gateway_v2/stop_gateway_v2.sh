#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8788}"
pkill -f "uvicorn main:app --host 127.0.0.1 --port $PORT" || true
