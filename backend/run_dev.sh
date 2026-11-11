#!/usr/bin/env bash
# Start the mock platform (9001) and the scheduler console (9000).
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

cleanup() {
  kill "${MOCK_PID:-0}" "${API_PID:-0}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uvicorn app.mock_server:root --host 127.0.0.1 --port 9001 &
MOCK_PID=$!

# wait until mock is up
for _ in $(seq 1 50); do
  curl -sf http://127.0.0.1:9001/mock/admin/state >/dev/null 2>&1 && break
  sleep 0.2
done

uvicorn app.main:app --host 127.0.0.1 --port 9000 &
API_PID=$!

wait
