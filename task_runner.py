from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from job_store import JobStore

logger = logging.getLogger("browser-agent.runner")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRunner:
    def __init__(
        self,
        store: JobStore,
        data_dir: Path,
        *,
        model: str,
        max_steps: int,
        task_timeout: int,
        get_api_key: Callable[[], str | None],
        openrouter_base_url: str,
    ):
        self.store = store
        self.data_dir = Path(data_dir)
        self.screenshots_dir = self.data_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.max_steps = max_steps
        self.task_timeout = task_timeout
        self.get_api_key = get_api_key
        self.openrouter_base_url = openrouter_base_url

        self._lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._current_job_id: str | None = None
        self._cancel_requested = False
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._queue_loop(), name="job-queue")

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)
        if not subs and job_id in self._subscribers:
            del self._subscribers[job_id]

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        for q in list(self._subscribers.get(job_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def emit(self, job_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.store.add_event(job_id, event_type, payload)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(job_id, event))
        except RuntimeError:
            pass
        return event

    def request_cancel(self, job_id: str) -> bool:
        if self._current_job_id != job_id:
            job = self.store.get_job(job_id)
            if job and job["status"] == "queued":
                self.store.update_job(
                    job_id,
                    status="cancelled",
                    error="Cancelado antes de iniciar",
                    finished_at=_utc_now(),
                )
                self.emit(job_id, "log", {"message": "Job cancelado en cola"})
                return True
            return False
        self._cancel_requested = True
        self.emit(job_id, "log", {"message": "Cancelación solicitada…"})
        return True

    async def _queue_loop(self) -> None:
        while True:
            try:
                active = self.store.get_active_job()
                if active and active["status"] == "queued":
                    await self._run_job(active["id"])
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("queue loop error")
                await asyncio.sleep(1)

    async def _run_job(self, job_id: str) -> None:
        async with self._lock:
            self._current_job_id = job_id
            self._cancel_requested = False
            self._current_task = asyncio.current_task()
            try:
                await self._execute(job_id)
            finally:
                self._current_job_id = None
                self._current_task = None
                self._cancel_requested = False

    async def _close_browser(self, browser: Any) -> None:
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

    async def _execute(self, job_id: str) -> None:
        from browser_use import Agent, Browser, ChatOpenAI

        job = self.store.get_job(job_id)
        if not job:
            return

        api_key = self.get_api_key()
        if not api_key:
            self.store.update_job(
                job_id,
                status="failed",
                error="OPENROUTER_API_KEY no configurada",
                finished_at=_utc_now(),
            )
            self.emit(job_id, "error", {"message": "OPENROUTER_API_KEY no configurada"})
            return

        self.store.update_job(
            job_id, status="running", started_at=_utc_now(), steps=0
        )
        self.emit(job_id, "log", {"message": f"Iniciando con modelo {self.model}"})

        browser = None
        step_count = 0

        try:
            browser = Browser(
                headless=False,
                window_size={"width": 1280, "height": 800},
                chromium_sandbox=False,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                ],
            )

            llm = ChatOpenAI(
                model=self.model,
                api_key=api_key,
                base_url=self.openrouter_base_url,
            )

            agent = Agent(task=job["task"], llm=llm, browser=browser)

            async def on_step_end(agent_inst: Agent) -> None:
                nonlocal step_count
                if self._cancel_requested:
                    raise asyncio.CancelledError("cancel requested")

                step_count += 1
                url = None
                thought = None
                actions_summary = None
                screenshot_path = None

                try:
                    url = agent_inst.browser_session.get_current_page_url()
                    if asyncio.iscoroutine(url):
                        url = await url
                except Exception:
                    try:
                        urls = agent_inst.history.urls()
                        url = urls[-1] if urls else None
                    except Exception:
                        url = None

                try:
                    thoughts = agent_inst.history.model_thoughts()
                    if thoughts:
                        t = thoughts[-1]
                        thought = str(getattr(t, "thinking", None) or t)[:500]
                except Exception:
                    pass

                try:
                    actions = agent_inst.history.model_actions()
                    if actions:
                        actions_summary = str(actions[-1])[:500]
                except Exception:
                    pass

                try:
                    from browser_use.browser.events import ScreenshotEvent

                    screenshot_event = agent_inst.browser_session.event_bus.dispatch(
                        ScreenshotEvent(full_page=False)
                    )
                    await screenshot_event
                    result = await screenshot_event.event_result(
                        raise_if_any=False, raise_if_none=False
                    )
                    b64 = None
                    if isinstance(result, dict):
                        b64 = result.get("screenshot") or result.get("data")
                    elif isinstance(result, str):
                        b64 = result
                    if b64:
                        raw = base64.b64decode(b64.split(",")[-1] if "," in b64 else b64)
                        shot = self.screenshots_dir / f"{job_id}.jpg"
                        shot.write_bytes(raw)
                        screenshot_path = str(shot)
                except Exception as exc:
                    logger.debug("screenshot failed: %s", exc)

                self.store.update_job(
                    job_id,
                    steps=step_count,
                    current_url=url,
                    last_screenshot=screenshot_path,
                )
                self.emit(
                    job_id,
                    "step",
                    {
                        "step": step_count,
                        "url": url,
                        "thought": thought,
                        "action": actions_summary,
                        "has_screenshot": bool(screenshot_path),
                    },
                )

            history = await asyncio.wait_for(
                agent.run(max_steps=self.max_steps, on_step_end=on_step_end),
                timeout=self.task_timeout,
            )

            result = history.final_result()
            steps = step_count
            try:
                if hasattr(history, "history"):
                    steps = max(steps, len(history.history))
            except Exception:
                pass

            self.store.update_job(
                job_id,
                status="completed",
                result=result,
                steps=steps,
                finished_at=_utc_now(),
            )
            self.emit(job_id, "result", {"result": result, "steps": steps})
            self.emit(job_id, "log", {"message": "Tarea completada"})

        except asyncio.TimeoutError:
            msg = f"Timeout tras {self.task_timeout}s"
            logger.error(msg)
            self.store.update_job(
                job_id, status="failed", error=msg, finished_at=_utc_now()
            )
            self.emit(job_id, "error", {"message": msg})
        except asyncio.CancelledError:
            msg = "Tarea cancelada"
            self.store.update_job(
                job_id, status="cancelled", error=msg, finished_at=_utc_now()
            )
            self.emit(job_id, "error", {"message": msg})
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            self.store.update_job(
                job_id,
                status="failed",
                error=str(exc),
                finished_at=_utc_now(),
            )
            self.emit(job_id, "error", {"message": str(exc)})
        finally:
            await self._close_browser(browser)
