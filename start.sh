#!/bin/sh
set -eu

cd /opt/bgutil-ytdlp-pot-provider/server/node_modules
deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts --host 127.0.0.1 --port 4416 >/tmp/bgutil-pot.log 2>&1 &
POT_PID=$!

cleanup() {
  kill "$POT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then
    echo "PO-token provider is ready on 127.0.0.1:4416"
    break
  fi
  i=$((i + 1))
  sleep 1
done

if [ "$i" -ge 30 ]; then
  echo "ERROR: YouTube PO-token provider did not start. Provider logs:"
  cat /tmp/bgutil-pot.log
  exit 1
fi

cd /app
exec gunicorn app:app --bind 0.0.0.0:"${PORT:-8080}" --workers 1 --threads 8 --timeout 0
