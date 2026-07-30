# browser-agent

- **Type:** PRODUCTION
- **Description:** Agente de navegador browser-use con UI robusta: jobs async, historial SQLite, progreso SSE y noVNC embebido por el mismo dominio. OpenRouter como LLM.
- **Stack:** Python 3.11, FastAPI + Uvicorn, browser-use + Playwright, SQLite, Xvfb + x11vnc + noVNC (proxy interno), Docker
- **Domain:** https://browser-agent.pages.therry.dev
- **Repo:** https://github.com/themikehage/browser-use-agent

## Arquitectura

- **POST /api/tasks** — encola job, responde al instante con `id`
- **GET /api/tasks/{id}/events** — SSE con steps, logs, resultado
- **SQLite** en `/data/jobs.db` — sobrevive refresh y reinicios
- **GET /vnc/** — reverse proxy HTTP+WS a websockify local :6080
- Frontend split: historial + log | noVNC embebido + screenshot fallback

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | UI |
| GET | `/health` | Healthcheck |
| GET | `/api/config` | Config pública |
| POST | `/api/tasks` | Crear job |
| GET | `/api/tasks` | Listar historial |
| GET | `/api/tasks/{id}` | Detalle + eventos |
| GET | `/api/tasks/{id}/events` | SSE progreso |
| GET | `/api/tasks/{id}/screenshot` | Último screenshot |
| POST | `/api/tasks/{id}/cancel` | Cancelar |
| * | `/vnc/*` | Proxy noVNC |

## Variables de entorno

| Variable | Default |
|----------|---------|
| `OPENROUTER_API_KEY` | requerida |
| `OPENROUTER_MODEL` | `openai/gpt-5.6-luna` |
| `AGENT_MAX_STEPS` | `30` |
| `AGENT_TASK_TIMEOUT` | `300` |
| `DATA_DIR` | `/data` |
| `APP_USERNAME` / `APP_PASSWORD` | auth sesión (login UI) |
| `APP_AUTH_TOKEN` | bearer opcional API |
| `AUTH_SECRET` | firma cookies |
| `VNC_VIEW_ONLY` | `true` |

## Local

```bash
cp .env.example .env
docker compose up --build
# http://localhost:8000
```
