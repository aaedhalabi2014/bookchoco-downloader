#!/bin/sh
set -eu

# Start the PO-token provider locally. v2.0.0 binds to localhost; keeping it
# local also avoids exposing its unauthenticated endpoint to the internet.
cd /opt/bgutil-ytdlp-pot-provider/server/node_modules

if [ -n "${YOUTUBE_PROXY_URL:-}" ]; then
  # The token provider must see the same egress IP as yt-dlp.
  HTTPS_PROXY="$YOUTUBE_PROXY_URL" \
  HTTP_PROXY="$YOUTUBE_PROXY_URL" \
  ALL_PROXY="$YOUTUBE_PROXY_URL" \
  deno run \
    --allow-env \
    --allow-net \
    --allow-ffi=. \
    --allow-read=. \
    ../src/main.ts \
    --host 127.0.0.1 \
    --port 4416 &
else
  deno run \
    --allow-env \
    --allow-net \
    --allow-ffi=. \
    --allow-read=. \
    ../src/main.ts \
    --host 127.0.0.1 \
    --port 4416 &
fi

# Wait briefly for the local provider before accepting download requests.
python - <<'PY'
import socket
import sys
import time

for _ in range(80):
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        if sock.connect_ex(("127.0.0.1", 4416)) == 0:
            sys.exit(0)
    finally:
        sock.close()
    time.sleep(0.25)

print("PO-token provider failed to start", file=sys.stderr)
sys.exit(1)
PY

cd /app
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "*"
