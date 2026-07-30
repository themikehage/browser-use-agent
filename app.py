import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from browser_use import Agent, Browser, ChatOpenAI

app = FastAPI()

class TaskRequest(BaseModel):
    task: str

task_lock = asyncio.Lock()
current_task_running = False

@app.post("/task")
async def run_task(req: TaskRequest):
    global current_task_running

    if current_task_running:
        raise HTTPException(status_code=409, detail="Ya hay una tarea en ejecución. Espera a que termine.")

    async with task_lock:
        current_task_running = True
        try:
            browser = Browser(
                headless=False,
                window_size={"width": 1280, "height": 800},
            )

            llm = ChatOpenAI(
                model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"),
                openai_api_key=os.environ["OPENROUTER_API_KEY"],
                openai_api_base="https://openrouter.ai/api/v1",
            )

            agent = Agent(
                task=req.task,
                llm=llm,
                browser=browser,
            )

            history = await agent.run()
            result = history.final_result()
            return {"result": result}
        finally:
            current_task_running = False

app.mount("/", StaticFiles(directory="static", html=True), name="static")
