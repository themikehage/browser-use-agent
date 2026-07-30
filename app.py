import asyncio
import logging
import os
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
import websockets

from auth import (
    authenticate_headers_cookies,
    authenticate_request,
    auth_enabled,
    bearer_token,
    clear_session_cookie,
    require_user,
    set_session_cookie,
    verify_credentials,
)
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
VNC_VIEW_ONLY = os.getenv("VNC_VIEW_ONLY", "true").lower() in ("1", "true", "yes")

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
http_client: httpx.AsyncClient | None = None


def _get_api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _display_ok() -> bool:
    display = os.environ.get("DISPLAY", "")
    if not display.startswith(":"):
        return False
    num = display[1:].split(".")[0]
    return Path(f"/tmp/.X11-unix/X{num}").exists()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "screenshots").mkdir(parents=True, exist_ok=True)
    http_client = httpx.AsyncClient(timeout=60.0, follow_redirects=False)

    for job in store.list_jobs(limit=20):
        if job["status"] == "running":
            store.update_job(
                job["id"],
                status="failed",
                error="Interrumpido por reinicio del servidor",
            )
    runner.start()

    if not auth_enabled() and not bearer_token():
        logger.warning("Auth disabled — set APP_USERNAME/APP_PASSWORD or APP_AUTH_TOKEN")
    if not _get_api_key():
        logger.warning("OPENROUTER_API_KEY not set")
    else:
        logger.info(
            "Startup OK | model=%s auth=%s vnc_view_only=%s data=%s",
            DEFAULT_MODEL,
            auth_enabled() or bool(bearer_token()),
            VNC_VIEW_ONLY,
            DATA_DIR,
        )
    yield
    await runner.stop()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="browser-agent", lifespan=lifespan)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public paths (HTML shell checks auth client-side)
        if path in ("/", "/login", "/health", "/api/auth/login", "/api/auth/status"):
            return await call_next(request)
        if path.startswith("/static/"):
            return await call_next(request)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        needs_auth = path.startswith("/api/") or path.startswith("/vnc")
        if not needs_auth:
            return await call_next(request)

        if not auth_enabled() and not bearer_token():
            return await call_next(request)

        user = authenticate_request(request)
        if user:
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "No autenticado"}, status_code=401)
        return RedirectResponse("/login", status_code=302)


app.add_middleware(AuthMiddleware)


class TaskCreate(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


@app.get("/health")
async def health():
    active = store.get_active_job()
    xvfb = _display_ok()
    vnc = _port_open("127.0.0.1", 6080)
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        disk_free_mb = round(usage.free / (1024 * 1024), 1)
        disk_ok = usage.free > 100 * 1024 * 1024
    except OSError:
        disk_free_mb = None
        disk_ok = False

    db_ok = True
    try:
        store.list_jobs(limit=1)
    except Exception:
        db_ok = False

    checks = {
        "api": True,
        "xvfb": xvfb,
        "vnc": vnc,
        "disk": disk_ok,
        "db": db_ok,
        "api_key": bool(_get_api_key()),
    }
    degraded = not all([xvfb, vnc, disk_ok, db_ok])
    status = "degraded" if degraded else "ok"

    return {
        "status": status,
        "checks": checks,
        "busy": bool(active),
        "active_job_id": active["id"] if active else None,
        "model": DEFAULT_MODEL,
        "display": os.environ.get("DISPLAY"),
        "vnc_path": "/vnc/vnc.html",
        "vnc_view_only": VNC_VIEW_ONLY,
        "disk_free_mb": disk_free_mb,
        "auth_required": auth_enabled() or bool(bearer_token()),
        "api_key_configured": bool(_get_api_key()),
    }


@app.get("/api/auth/status")
async def auth_status(request: Request):
    user = authenticate_request(request)
    return {
        "auth_required": auth_enabled() or bool(bearer_token()),
        "authenticated": bool(user)
        and not (user and user.via == "open" and auth_enabled()),
        "user": user.username if user and user.via != "open" else None,
        "via": user.via if user else None,
    }


@app.post("/api/auth/login")
async def login(body: LoginBody, response: Response):
    if not auth_enabled():
        # If only bearer token configured, reject password login
        if bearer_token():
            raise HTTPException(400, "Usa Bearer token (APP_AUTH_TOKEN)")
        raise HTTPException(400, "Auth no configurada")
    if not verify_credentials(body.username, body.password):
        raise HTTPException(401, "Credenciales inválidas")
    set_session_cookie(response, body.username)
    return {"ok": True, "user": body.username}


@app.post("/api/auth/logout")
async def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/config")
async def config(request: Request):
    require_user(request)
    return {
        "model": DEFAULT_MODEL,
        "max_steps": MAX_STEPS,
        "task_timeout": TASK_TIMEOUT,
        "vnc_path": "/vnc/vnc.html?autoconnect=1&resize=scale&view_only=1"
        if VNC_VIEW_ONLY
        else "/vnc/vnc.html?autoconnect=1&resize=scale",
        "vnc_view_only": VNC_VIEW_ONLY,
        "busy": bool(store.get_active_job()),
        "api_key_configured": bool(_get_api_key()),
        "auth_required": auth_enabled() or bool(bearer_token()),
    }


@app.post("/api/tasks")
async def create_task(body: TaskCreate, request: Request):
    require_user(request)
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
async def list_tasks(request: Request, limit: int = 50):
    require_user(request)
    return {"jobs": store.list_jobs(limit=min(limit, 100))}


@app.get("/api/tasks/{job_id}")
async def get_task(job_id: str, request: Request):
    require_user(request)
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    events = store.list_events(job_id, after_id=0, limit=500)
    return {"job": job, "events": events}


@app.get("/api/tasks/{job_id}/screenshot")
async def get_screenshot(job_id: str, request: Request):
    require_user(request)
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    path = job.get("last_screenshot")
    if not path or not Path(path).is_file():
        fallback = DATA_DIR / "screenshots" / f"{job_id}.jpg"
        if not fallback.is_file():
            raise HTTPException(404, "Sin screenshot")
        path = str(fallback)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/tasks/{job_id}/cancel")
async def cancel_task(job_id: str, request: Request):
    require_user(request)
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(400, f"Job ya está en estado {job['status']}")
    ok = await runner.cancel_hard(job_id)
    if not ok:
        # fallback sync path
        ok = runner.request_cancel(job_id)
    if not ok:
        raise HTTPException(400, "No se pudo cancelar")
    return {"ok": True, "id": job_id}


@app.get("/api/tasks/{job_id}/events")
async def task_events(job_id: str, request: Request, after: int = 0):
    require_user(request)
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")

    async def event_stream():
        last_id = after
        for ev in store.list_events(job_id, after_id=last_id):
            last_id = ev["id"]
            yield _sse(ev)

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
    client = http_client or httpx.AsyncClient(timeout=60.0)

    try:
        req = client.build_request(
            request.method, upstream, headers=headers, content=body
        )
        upstream_res = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        logger.warning("VNC proxy error: %s", exc)
        return JSONResponse(
            {"detail": f"VNC upstream unavailable: {exc}"}, status_code=502
        )

    excluded = {
        "content-encoding",
        "transfer-encoding",
        "content-length",
        "connection",
    }
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

    return StreamingResponse(
        stream(),
        status_code=upstream_res.status_code,
        headers=out_headers,
    )


@app.api_route("/vnc", methods=["GET", "HEAD"])
async def vnc_root(request: Request):
    require_user(request)
    return await _proxy_vnc_http(request, "vnc.html")


@app.api_route("/vnc/{path:path}", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def vnc_http(request: Request, path: str):
    require_user(request)
    return await _proxy_vnc_http(request, path)


@app.websocket("/vnc/{path:path}")
async def vnc_ws(websocket: WebSocket, path: str):
    if auth_enabled() or bearer_token():
        user = authenticate_headers_cookies(
            websocket.headers,
            dict(websocket.cookies),
            query_token=websocket.query_params.get("token"),
        )
        if not user:
            await websocket.close(code=4401)
            return

    qs = websocket.url.query
    clean = path.lstrip("/")
    if clean.startswith("vnc/"):
        clean = clean[4:]
    target = f"{VNC_WS_UPSTREAM.rstrip('/')}/{clean or 'websockify'}"
    if qs:
        target = f"{target}?{qs}"

    client_subprotocols = list(websocket.scope.get("subprotocols") or [])
    try:
        connect_kwargs: dict = {
            "open_timeout": 10,
            "max_size": 8 * 1024 * 1024,
        }
        if client_subprotocols:
            connect_kwargs["subprotocols"] = client_subprotocols

        async with websockets.connect(target, **connect_kwargs) as upstream:
            accepted_sub = None
            if client_subprotocols:
                accepted_sub = (
                    getattr(upstream, "subprotocol", None) or client_subprotocols[0]
                )
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

            _done, pending = await asyncio.wait(
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
            if websocket.client_state.name != "CONNECTED":
                await websocket.accept()
            await websocket.close(code=1011)
        except Exception:
            pass


@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
