# Design: Robust Agent UI (jobs + VNC embebido + historial)

**Date:** 2026-07-30  
**Status:** Approved for implementation  
**Approach:** A — async jobs + noVNC same-origin + SQLite (+ last-step screenshot fallback)

## Problems

1. Live browser unreachable (`:6080` not on HTTPS proxy)
2. No agent progress while task runs (blocking `POST /task`)
3. Page reload loses all client state

## Architecture

- Single public port `8000`
- `POST /api/tasks` → `{id}` immediately; work in background
- SSE `GET /api/tasks/{id}/events` for live steps
- SQLite `/data/jobs.db` for jobs + event log (survives refresh/restart)
- Reverse proxy `/vnc/*` → `127.0.0.1:6080` (HTTP + WebSocket)
- Frontend: split layout — task form/history/log + embedded noVNC iframe
- `on_step_end` hook: append step event + optional screenshot fallback

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/api/config` | model, vnc path, limits |
| POST | `/api/tasks` | `{task}` → `{id, status}` |
| GET | `/api/tasks` | list recent jobs |
| GET | `/api/tasks/{id}` | job detail + steps summary |
| GET | `/api/tasks/{id}/events` | SSE stream |
| GET | `/api/tasks/{id}/screenshot` | last screenshot image |
| POST | `/api/tasks/{id}/cancel` | request cancel (best-effort) |
| ALL | `/vnc/{path}` | proxy to noVNC/websockify |

## Job lifecycle

`queued` → `running` → `completed` | `failed` | `cancelled`

One job runs at a time (global lock). New posts while busy → `queued` or 409; **queue of 1** optional — MVP: reject with 409 if busy/queued.

## Persistence

- SQLite WAL under `DATA_DIR` (default `/data`)
- Screenshots under `DATA_DIR/screenshots/{job_id}.jpg`
- Keep last 100 jobs (prune on insert)

## Frontend

- On load: `GET /api/tasks` + reconnect SSE if any `running`/`queued`
- noVNC iframe: `/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/`
- If iframe errors: show last screenshot
- localStorage only for UI prefs (selected job id), source of truth is API

## Out of scope (later)

- Multi-worker / multi-browser parallel
- Auth on VNC
- Full job cancel mid-Playwright step
