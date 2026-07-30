#!/bin/bash
Xvfb :99 -screen 0 1280x800x24 &
sleep 1
fluxbox &
x11vnc -display :99 -forever -nopw -quiet &
websockify --web=/usr/share/novnc 6080 localhost:5900 &
export DISPLAY=:99
uvicorn app:app --host 0.0.0.0 --port 8000
