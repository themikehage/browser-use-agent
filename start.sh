#!/bin/bash
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

cleanup() {
  echo "[start] shutting down..."
  kill $(jobs -p) 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[start] Xvfb on DISPLAY=${DISPLAY}"
Xvfb "${DISPLAY}" -screen 0 1280x800x24 -ac +extension GLX +render -noreset &
sleep 1

if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  echo "[start] WARNING: xdpyinfo not available or display not ready, continuing..."
fi

echo "[start] fluxbox"
fluxbox >/tmp/fluxbox.log 2>&1 &

echo "[start] x11vnc on :5900"
x11vnc -display "${DISPLAY}" -forever -shared -rfbport 5900 -nopw -quiet -xkb &

echo "[start] websockify/noVNC on :6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 &

echo "[start] uvicorn on :8000"
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
