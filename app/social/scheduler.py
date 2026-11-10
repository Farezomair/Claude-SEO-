"""Background loop inside the Ascend web service: plan every 30 minutes, publish what's due.
Runs when SOCIAL_ENABLED (or, if that is unset, OUTREACH_ENABLED) is true. It only plans once settings are
uploaded, and only posts when the Social page's autopilot is switched on and the page is connected."""
import os
import threading
import time

from . import planner, store

TICK_SECONDS = 300
ENABLED = (os.getenv("SOCIAL_ENABLED") or os.getenv("OUTREACH_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
_started = False


def _loop():
    planner._log("scheduler started")
    while True:
        planner.run_once()
        time.sleep(TICK_SECONDS)


def start():
    global _started
    if _started or not ENABLED:
        return
    _started = True
    store.init()
    threading.Thread(target=_loop, daemon=True).start()
