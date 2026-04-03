"""
start.py — Single-process launcher
====================================
Runs collector + dashboard in ONE async event loop. No threads.

Railway needs a bound port (uvicorn) to stay alive. The collector
runs as async tasks in the same loop — if one crashes, we know about it.
"""
import asyncio
import os
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("apex.start")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


async def run_collector():
    """Run the data collector (sources + consumer) as async tasks."""
    import main as apex_main
    await apex_main.main()


def main():
    """Start everything in one process, one event loop."""
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    # Configure uvicorn to run in an existing event loop
    config = uvicorn.Config(
        "dashboard:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="warning",  # reduce uvicorn noise
    )
    server = uvicorn.Server(config)

    async def run_all():
        # Start collector as a task (not a thread)
        collector = asyncio.create_task(run_collector())
        log.info("Collector started as async task")

        # Start uvicorn as a task in the same loop
        await server.serve()

        # If uvicorn exits, cancel collector
        collector.cancel()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
