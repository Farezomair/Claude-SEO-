"""In-process weekly scheduler.

A daemon thread wakes every SCHEDULER_CHECK_MINUTES and starts a weekly run for
any site whose last weekly run is older than WEEKLY_INTERVAL_DAYS (or has none).
This keeps everything in the single Railway web service — no separate cron job
to configure. The work itself (audit → fix → report) is the weekly conductor in
weekly.py.

Resume-safe: due-ness is read from the database each tick, so a restart just
re-checks and continues. Set WEEKLY_ENABLED=false to turn the timer off.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .database import SessionLocal
from .models import JobRun, Site
from .weekly import run_weekly

CHECK_MINUTES = int(os.getenv("SCHEDULER_CHECK_MINUTES", "180"))
INTERVAL_DAYS = int(os.getenv("WEEKLY_INTERVAL_DAYS", "7"))
ENABLED = os.getenv("WEEKLY_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

_started = False


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_weekly(db, site_id: int) -> JobRun | None:
    return (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "weekly")
        .order_by(JobRun.created_at.desc())
        .first()
    )


def is_due(db, site_id: int) -> bool:
    last = _last_weekly(db, site_id)
    if last is None:
        return True
    if last.status == "running":
        return False
    ts = last.created_at
    if ts is None:
        return True
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return ts < (_now_naive() - timedelta(days=INTERVAL_DAYS))


def _tick() -> None:
    db = SessionLocal()
    try:
        for site in db.query(Site).all():
            if is_due(db, site.id):
                run = JobRun(site_id=site.id, kind="weekly", status="running", summary="Weekly run starting…")
                db.add(run)
                db.commit()
                db.refresh(run)
                threading.Thread(target=run_weekly, args=(site.id, run.id), daemon=True).start()
    finally:
        db.close()


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:
            pass  # never let the scheduler thread die
        time.sleep(CHECK_MINUTES * 60)


def start_scheduler() -> None:
    global _started
    if _started or not ENABLED:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
