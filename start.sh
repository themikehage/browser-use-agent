#!/bin/bash
set -uo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

cleanup() {
  echo "[start] shutting down..."
  # Don't kill uvicorn if we already exec'd into it; only background jobs
  jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Free stale lock from previous crashed Xvfb
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

echo "[start] Xvfb on DISPLAY=${DISPLAY}"
Xvfb "${DISPLAY}" -screen 0 1280x800x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# Wait until the display is actually ready (up to ~15s)
ready=0
for i in $(seq 1 30); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    ready=1
    echo "[start] X display ready after ${i}00ms"
    break
  fi
  # Fallback: process alive + socket exists
  if kill -0 "$XVFB_PID" 2>/dev/null && [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    ready=1
    echo "[start] X socket present (xdpyinfo optional)"
    break
  fi
  sleep 0.5
done

if [ "$ready" -ne 1 ]; then
  echo "[start] ERROR: Xvfb failed to start. Log:"
  cat /tmp/xvfb.log 2>/dev/null || true
  # Still start API so /health works; browser tasks will fail clearly
fi

echo "[start] fluxbox"
fluxbox >/tmp/fluxbox.log 2>&1 &

echo "[start] x11vnc on :5900"
x11vnc -display "${DISPLAY}" -forever -shared -rfbport 5900 -nopw -quiet -xkb >/tmp/x11vnc.log 2>&1 &

echo "[start] websockify/noVNC on :6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

# Clear EXIT trap before exec so we don't kill children incorrectly;
# Docker SIGTERM will stop the container PID (uvicorn).
trap - EXIT

echo "[start] uvicorn on :8000"
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
