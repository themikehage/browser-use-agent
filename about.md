# browser-agent

- **Type:** PRODUCTION
- **Description:** Agente de navegador browser-use en contenedor único con display virtual Xvfb + VNC/noVNC. Frontend web para enviar tareas y ver el navegador en vivo. Usa OpenRouter como proveedor LLM.
- **Stack:** Python 3.11, FastAPI + Uvicorn, browser-use + Playwright, ChatOpenAI→OpenRouter, Xvfb + x11vnc + noVNC, Docker
- **Domain:** https://browser-agent.pages.therry.dev
- **VNC:** http://browser-agent.pages.therry.dev:6080/vnc.html
- **Repo:** https://github.com/themikehage/browser-use-agent

## Arquitectura

Contenedor único con:
- **app.py** — FastAPI: frontend estático, `GET /health`, `GET /config`, `POST /task`
- **Xvfb :99** — display virtual 1280x800
- **fluxbox** — window manager ligero
- **x11vnc** — servidor VNC sobre display :99 (puerto 5900)
- **websockify** — puente WebSocket para noVNC (puerto 6080)
- **uvicorn** — servidor ASGI (puerto 8000), 1 worker

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Frontend HTML |
| GET | `/health` | Healthcheck (status, busy, model) |
| GET | `/config` | Config pública (modelo, VNC URL, timeouts) |
| POST | `/task` | Ejecuta tarea con browser-use (lock exclusivo) |

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | API key de OpenRouter | **requerida** |
| `OPENROUTER_MODEL` | Modelo LLM | `openai/gpt-5.6-luna` |
| `AGENT_MAX_STEPS` | Máx. pasos del agente | `30` |
| `AGENT_TASK_TIMEOUT` | Timeout tarea (segundos) | `300` |
| `VNC_PUBLIC_URL` | URL pública noVNC para el frontend | vacío (fallback host:6080) |

## Estabilidad (fixes aplicados)

- `ChatOpenAI` con `base_url` OpenRouter (patrón oficial browser-use)
- Cierre de browser en `finally` (anti-OOM)
- Lock atómico + 409 si hay tarea en curso
- Timeout + max_steps
- Validación de API key al startup
- `shm_size: 2gb`, chromium sin sandbox en Docker
- HEALTHCHECK en imagen y compose
- Frontend con manejo de errores y VNC configurable
- Dependencias con rangos de versión

## Despliegue local

```bash
cp .env.example .env
# editar OPENROUTER_API_KEY
docker compose up --build
# App: http://localhost:8000
# VNC: http://localhost:6080/vnc.html
```

## Mejoras futuras

- Jobs asíncronos (POST devuelve job_id + polling/SSE)
- Auth en VNC / no exponer 6080 en internet
- Process supervisor (supervisord/s6) si Xvfb muere
- Cancelación de tareas en curso
