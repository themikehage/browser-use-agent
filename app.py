import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
import websockets

from job_store import JobStore
from task_runner import TaskRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("browser-agent")

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "30"))
TASK_TIMEOUT = int(os.getenv("AGENT_TASK_TIMEOUT", "300"))
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-5.6-luna")
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
VNC_UPSTREAM = os.getenv("VNC_UPSTREAM", "http://127.0.0.1:6080")
VNC_WS_UPSTREAM = os.getenv("VNC_WS_UPSTREAM", "ws://127.0.0.1:6080")

store = JobStore(DATA_DIR / "jobs.db")
runner = TaskRunner(
    store,
    DATA_DIR,
    model=DEFAULT_MODEL,
    max_steps=MAX_STEPS,
    task_timeout=TASK_TIMEOUT,
    get_api_key=lambda: os.environ.get("OPENROUTER_API_KEY")
    or os.environ.get("OPENAI_API_KEY"),
    openrouter_base_url=OPENROUTER_BASE_URL,
)


def _get_api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "screenshots").mkdir(parents=True, exist_ok=True)
    # Recover stuck jobs from previous crash
    for job in store.list_jobs(limit=20):
        if job["status"] == "running":
            store.update_job(
                job["id"],
                status="failed",
                error="Interrumpido por reinicio del servidor",
            )
    runner.start()
    if not _get_api_key():
        logger.warning("OPENROUTER_API_KEY not set")
    else:
        logger.info(
            "Startup OK | model=%s max_steps=%s timeout=%ss data=%s",
            DEFAULT_MODEL,
            MAX_STEPS,
            TASK_TIMEOUT,
            DATA_DIR,
        )
    yield
    await runner.stop()


app = FastAPI(title="browser-agent", lifespan=lifespan)


class TaskCreate(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)


@app.get("/health")
async def health():
    active = store.get_active_job()
    return {
        "status": "ok",
        "busy": bool(active),
        "active_job_id": active["id"] if active else None,
        "model": DEFAULT_MODEL,
        "display": os.environ.get("DISPLAY"),
        "vnc_path": "/vnc/vnc.html",
        "api_key_configured": bool(_get_api_key()),
    }


@app.get("/api/config")
async def config():
    return {
        "model": DEFAULT_MODEL,
        "max_steps": MAX_STEPS,
        "task_timeout": TASK_TIMEOUT,
        "vnc_path": "/vnc/vnc.html?autoconnect=1&resize=scale",
        "busy": bool(store.get_active_job()),
        "api_key_configured": bool(_get_api_key()),
    }


@app.post("/api/tasks")
async def create_task(body: TaskCreate):
    if not _get_api_key():
        raise HTTPException(500, "OPENROUTER_API_KEY no configurada")
    active = store.get_active_job()
    if active:
        raise HTTPException(
            409,
            detail={
                "message": "Ya hay una tarea en cola o en ejecución",
                "active_job_id": active["id"],
                "status": active["status"],
            },
        )
    job = store.create_job(body.task.strip())
    runner.emit(job["id"], "log", {"message": "Job encolado"})
    return job


@app.get("/api/tasks")
async def list_tasks(limit: int = 50):
    return {"jobs": store.list_jobs(limit=min(limit, 100))}


@app.get("/api/tasks/{job_id}")
async def get_task(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    events = store.list_events(job_id, after_id=0, limit=500)
    return {"job": job, "events": events}


@app.get("/api/tasks/{job_id}/screenshot")
async def get_screenshot(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    path = job.get("last_screenshot")
    if not path or not Path(path).is_file():
        # try default path
        fallback = DATA_DIR / "screenshots" / f"{job_id}.jpg"
        if not fallback.is_file():
            raise HTTPException(404, "Sin screenshot")
        path = str(fallback)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/tasks/{job_id}/cancel")
async def cancel_task(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(400, f"Job ya está en estado {job['status']}")
    ok = runner.request_cancel(job_id)
    if not ok:
        raise HTTPException(400, "No se pudo cancelar")
    return {"ok": True, "id": job_id}


@app.get("/api/tasks/{job_id}/events")
async def task_events(job_id: str, request: Request, after: int = 0):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")

    async def event_stream():
        last_id = after
        # Replay stored events first
        for ev in store.list_events(job_id, after_id=last_id):
            last_id = ev["id"]
            yield _sse(ev)

        # If already terminal, close after replay
        fresh = store.get_job(job_id)
        if fresh and fresh["status"] in ("completed", "failed", "cancelled"):
            yield _sse_raw("done", {"status": fresh["status"]})
            return

        q = runner.subscribe(job_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    last_id = max(last_id, ev.get("id", last_id))
                    yield _sse(ev)
                    if ev.get("type") in ("result", "error") or (
                        ev.get("type") == "status"
                        and ev.get("payload", {}).get("status")
                        in ("completed", "failed", "cancelled")
                    ):
                        # drain a moment then end
                        await asyncio.sleep(0.1)
                        fresh = store.get_job(job_id)
                        if fresh and fresh["status"] in (
                            "completed",
                            "failed",
                            "cancelled",
                        ):
                            yield _sse_raw("done", {"status": fresh["status"]})
                            break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    fresh = store.get_job(job_id)
                    if fresh and fresh["status"] in (
                        "completed",
                        "failed",
                        "cancelled",
                    ):
                        # catch events written while we waited
                        for ev in store.list_events(job_id, after_id=last_id):
                            last_id = ev["id"]
                            yield _sse(ev)
                        yield _sse_raw("done", {"status": fresh["status"]})
                        break
        finally:
            runner.unsubscribe(job_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: dict) -> str:
    import json

    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event.get('type', 'message')}\ndata: {data}\n\n"


def _sse_raw(event_type: str, payload: dict) -> str:
    import json

    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── VNC reverse proxy (HTTP) ──────────────────────────────────────────

async def _proxy_vnc_http(request: Request, path: str) -> Response:
    upstream = f"{VNC_UPSTREAM.rstrip('/')}/{path}"
    if request.url.query:
        upstream = f"{upstream}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "connection", "content-length")
    }
    body = await request.body()

    client = httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        req = client.build_request(
            request.method, upstream, headers=headers, content=body
        )
        upstream_res = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning("VNC proxy error: %s", exc)
        return JSONResponse(
            {"detail": f"VNC upstream unavailable: {exc}"}, status_code=502
        )

    excluded = {"content-encoding", "transfer-encoding", "content-length", "connection"}
    out_headers = {
        k: v
        for k, v in upstream_res.headers.items()
        if k.lower() not in excluded
    }

    async def stream():
        try:
            async for chunk in upstream_res.aiter_raw():
                yield chunk
        finally:
            await upstream_res.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        status_code=upstream_res.status_code,
        headers=out_headers,
        background=BackgroundTask(lambda: None),
    )


@app.api_route("/vnc", methods=["GET", "HEAD"])
async def vnc_root(request: Request):
    return await _proxy_vnc_http(request, "vnc.html")


@app.api_route("/vnc/{path:path}", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def vnc_http(request: Request, path: str):
    return await _proxy_vnc_http(request, path)


@app.websocket("/vnc/{path:path}")
async def vnc_ws(websocket: WebSocket, path: str):
    qs = websocket.url.query
    # noVNC default path is "websockify"; strip accidental prefixes
    clean = path.lstrip("/")
    if clean.startswith("vnc/"):
        clean = clean[4:]
    target = f"{VNC_WS_UPSTREAM.rstrip('/')}/{clean or 'websockify'}"
    if qs:
        target = f"{target}?{qs}"

    client_subprotocols = list(websocket.scope.get("subprotocols") or [])
    try:
        connect_kwargs = {
            "open_timeout": 10,
            "max_size": 8 * 1024 * 1024,
        }
        if client_subprotocols:
            connect_kwargs["subprotocols"] = client_subprotocols

        async with websockets.connect(target, **connect_kwargs) as upstream:
            accepted_sub = None
            if client_subprotocols:
                # pick first negotiated if any
                accepted_sub = getattr(upstream, "subprotocol", None) or client_subprotocols[0]
            await websocket.accept(subprotocol=accepted_sub)

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as exc:
        logger.warning("VNC websocket proxy failed (%s): %s", target, exc)
        try:
            # accept+close so browser sees failure instead of hanging
            if websocket.client_state.name != "CONNECTED":
                await websocket.accept()
            await websocket.close(code=1011)
        except Exception:
            pass


# Legacy aliases
@app.post("/task")
async def legacy_task(body: TaskCreate):
    """Back-compat: still accepts sync-style clients but returns job id quickly."""
    return await create_task(body)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
