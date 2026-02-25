#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8788}"
HOST="${HOST:-127.0.0.1}"

curl -sS "http://$HOST:$PORT/health"
