import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from browser_use import Agent, Browser, ChatOpenRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("browser-agent")

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "30"))
TASK_TIMEOUT = int(os.getenv("AGENT_TASK_TIMEOUT", "300"))
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
VNC_PUBLIC_URL = os.getenv("VNC_PUBLIC_URL", "")

task_lock = asyncio.Lock()
current_task_running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.error("OPENROUTER_API_KEY is not set")
        raise RuntimeError("OPENROUTER_API_KEY environment variable is required")
    logger.info(
        "Startup OK | model=%s max_steps=%s timeout=%ss",
        DEFAULT_MODEL,
        MAX_STEPS,
        TASK_TIMEOUT,
    )
    yield


app = FastAPI(title="browser-agent", lifespan=lifespan)


class TaskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)


class TaskResponse(BaseModel):
    result: str | None
    success: bool
    steps: int | None = None


class HealthResponse(BaseModel):
    status: str
    busy: bool
    model: str
    display: str | None
    vnc_url: str | None


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        busy=current_task_running,
        model=DEFAULT_MODEL,
        display=os.environ.get("DISPLAY"),
        vnc_url=VNC_PUBLIC_URL or None,
    )


@app.get("/config")
async def config():
    return {
        "model": DEFAULT_MODEL,
        "max_steps": MAX_STEPS,
        "task_timeout": TASK_TIMEOUT,
        "vnc_url": VNC_PUBLIC_URL or None,
        "busy": current_task_running,
    }


async def _close_browser(browser: Browser | None) -> None:
    if browser is None:
        return
    for method_name in ("kill", "stop", "close"):
        method = getattr(browser, method_name, None)
        if method is None:
            continue
        try:
            result = method()
            if asyncio.iscoroutine(result):
                await result
            logger.info("Browser closed via %s()", method_name)
            return
        except Exception as exc:
            logger.warning("browser.%s() failed: %s", method_name, exc)


@app.post("/task", response_model=TaskResponse)
async def run_task(req: TaskRequest):
    global current_task_running

    if current_task_running or task_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Ya hay una tarea en ejecución. Espera a que termine.",
        )

    async with task_lock:
        if current_task_running:
            raise HTTPException(
                status_code=409,
                detail="Ya hay una tarea en ejecución. Espera a que termine.",
            )

        current_task_running = True
        browser: Browser | None = None

        try:
            logger.info("Starting task: %s", req.task[:200])

            browser = Browser(
                headless=False,
                window_size={"width": 1280, "height": 800},
                chromium_sandbox=False,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
            )

            llm = ChatOpenRouter(model=DEFAULT_MODEL)

            agent = Agent(
                task=req.task,
                llm=llm,
                browser=browser,
            )

            history = await asyncio.wait_for(
                agent.run(max_steps=MAX_STEPS),
                timeout=TASK_TIMEOUT,
            )

            result = history.final_result()
            steps = None
            try:
                steps = len(history.history) if hasattr(history, "history") else None
            except Exception:
                steps = None

            logger.info(
                "Task finished | steps=%s result_len=%s",
                steps,
                len(result or ""),
            )
            return TaskResponse(result=result, success=True, steps=steps)

        except asyncio.TimeoutError:
            logger.error("Task timed out after %ss", TASK_TIMEOUT)
            raise HTTPException(
                status_code=504,
                detail=f"La tarea superó el timeout de {TASK_TIMEOUT}s",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Task failed")
            raise HTTPException(
                status_code=500,
                detail=f"Error ejecutando tarea: {exc}",
            )
        finally:
            await _close_browser(browser)
            current_task_running = False


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
