"""
start.py — Launch both APEX collector and dashboard in the same process.
Railway runs a single service, so we run main.py as a background task
and uvicorn as the foreground (Railway needs a bound port to stay alive).
"""
import asyncio
import os
import threading
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def run_collector():
    """Run the APEX data collector (main.py logic) in a background thread."""
    import main as apex_main
    asyncio.run(apex_main.main())


def run_dashboard():
    """Run the FastAPI dashboard server (foreground — Railway needs a bound port)."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    # Start collector in background thread
    collector_thread = threading.Thread(target=run_collector, daemon=True, name="apex-collector")
    collector_thread.start()

    # Run dashboard in foreground (this blocks until process exits)
    run_dashboard()
