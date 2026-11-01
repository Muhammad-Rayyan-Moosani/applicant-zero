"""Job Application Engine — master runtime.

Runs three things concurrently under one asyncio loop:
  1. the FastAPI dark dashboard (uvicorn)          → http://<tailscale-ip>:8000
  2. the Telegram long-poll gateway (Agent 8)      → remote control + human gates
  3. the agent pipeline                            → kicked off by a URL (Telegram or CLI)

Usage:
  python main.py                 # start servers, drive everything from Telegram
  python main.py <job_url>       # also start a pipeline run immediately
"""
from __future__ import annotations

import asyncio
import sys

import uvicorn

from src import config, telegram_gate
from src.graph import Swarm
from src.dashboard import app as dashboard_app
from src.state import STATE

_manager = Swarm()                 # the LangGraph decision-loop swarm
_loop: asyncio.AbstractEventLoop


def _start_pipeline(url: str) -> None:
    """Thread-safe entry point (called from the Telegram poll thread)."""
    asyncio.run_coroutine_threadsafe(_manager.run(url), _loop)


async def _serve_dashboard() -> None:
    cfg = uvicorn.Config(dashboard_app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                         log_level="warning")
    await uvicorn.Server(cfg).serve()


async def main() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    STATE.loop = _loop

    if not config.ANTHROPIC_API_KEY:
        print("!! ANTHROPIC_API_KEY missing — copy .env.example to .env and set it.")
    print(f"Dashboard → http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}  "
          f"(reach it across Tailscale at http://<your-tailscale-ip>:{config.DASHBOARD_PORT})")

    telegram_gate.set_source_handler(_start_pipeline)
    telegram_gate.start()                      # background daemon thread

    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        asyncio.create_task(_manager.run(sys.argv[1]))

    await _serve_dashboard()                    # blocks forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
