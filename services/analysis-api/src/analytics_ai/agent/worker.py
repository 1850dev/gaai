"""Run due reports once (Cloud Run Job/cron) or continuously (local Docker)."""

from __future__ import annotations

import argparse
import asyncio
import logging

from analytics_ai.agent.schedules import ScheduleService
from analytics_ai.api.app import build_services
from analytics_ai.config import Settings


async def run(loop: bool) -> None:
    settings = Settings()
    if settings.resolved_repository_backend != "firestore":
        raise RuntimeError("scheduled_worker_requires_firestore")
    if settings.resolved_cache_backend == "memory":
        raise RuntimeError("scheduled_reports_require_durable_cache")
    if not settings.enable_analytics_tools:
        raise RuntimeError("analytics_tools_disabled")
    services = build_services(settings)
    scheduler = ScheduleService(services.agent)
    while True:
        try:
            await scheduler.run_due()
        except Exception:
            logging.getLogger(__name__).error("scheduled worker tick failed")
            if not loop:
                raise
        if not loop:
            break
        await asyncio.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    asyncio.run(run(parser.parse_args().loop))
