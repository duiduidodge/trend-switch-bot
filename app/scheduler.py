from __future__ import annotations

import asyncio
import logging

from app.service import TrendSwitchService

logger = logging.getLogger(__name__)


async def _loop_runner(interval_seconds: int, job_name: str, job) -> None:
    while True:
        try:
            job()
        except Exception:
            logger.exception("Scheduler job failed: %s", job_name)
        await asyncio.sleep(interval_seconds)


def start_scheduler(service: TrendSwitchService) -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(
            _loop_runner(service.settings.signal_scan_interval_minutes * 60, "signals", service.run_signals)
        ),
        asyncio.create_task(
            _loop_runner(service.settings.monitor_interval_minutes * 60, "monitor", service.run_monitor)
        ),
    ]
    reporter = getattr(service, "noon_hub_reporter", None)
    if reporter is not None and reporter.enabled:
        tasks.append(asyncio.create_task(_loop_runner(60, "hub-heartbeat", reporter.publish_heartbeat)))
        tasks.append(asyncio.create_task(_loop_runner(60, "hub-snapshot", reporter.publish_snapshot)))
    return tasks
