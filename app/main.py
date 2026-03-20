from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import BotDatabase
from app.hyperliquid_client import HyperliquidClient
from app.models import Asset
from app.noon_hub import NoonHubReporter
from app.scheduler import start_scheduler
from app.service import TrendSwitchService

logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO))

settings = get_settings()
db = BotDatabase(settings.db_path())
client = HyperliquidClient(settings)
service = TrendSwitchService(settings, db, client)
service.noon_hub_reporter = NoonHubReporter(settings, service)
scheduler_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_tasks
    if service.noon_hub_reporter.enabled:
        asyncio.create_task(asyncio.to_thread(service.noon_hub_reporter.register_bot))
        asyncio.create_task(
            asyncio.to_thread(service.noon_hub_reporter.publish_heartbeat, message="Trend Switch Bot startup")
        )
        asyncio.create_task(asyncio.to_thread(service.noon_hub_reporter.publish_snapshot))
    if settings.enable_scheduler:
        scheduler_tasks = start_scheduler(service)
    yield
    for task in scheduler_tasks:
        task.cancel()


app = FastAPI(title="Trend Switch Bot", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run/signals")
def run_signals() -> list[dict]:
    return service.run_signals()


@app.post("/run/monitor")
def run_monitor() -> list[dict]:
    return service.run_monitor()


@app.get("/api/dashboard")
def api_dashboard() -> dict:
    return service.dashboard_data()


@app.post("/api/run/signals")
def api_run_signals() -> list[dict]:
    return service.run_signals()


@app.post("/api/run/monitor")
def api_run_monitor() -> list[dict]:
    return service.run_monitor()


@app.post("/api/positions/{asset}/close")
def api_close_position(asset: str) -> dict:
    try:
        parsed_asset = Asset(asset.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unsupported asset") from exc
    return service.close_position(parsed_asset)


@app.get("/state")
def state() -> dict:
    return service.state()
