"""Outreach background loop, running inside the Ascend web service on Railway.

Every few minutes it checks the clock (US Eastern) and does whatever is due:
  inbox     every 15 minutes: read replies, act on them, alert the owner
  prepare   once a day from 06:00 ET on first-email days: write + score emails for researched prospects
  send      during the send window: first emails, follow-ups, threaded, spaced, within limits
  digest    once a day after 12:30 ET: email the owner a summary
Everything is read from the database each tick, so a restart or redeploy simply continues.
Set OUTREACH_ENABLED=true in Railway to turn it on.
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import config, db, digest, inbox, sequence, serp, writer

TICK_SECONDS = 120
_started = False
_send_lock = threading.Lock()
_work_lock = threading.Lock()


def _log(msg):
    print(f"[outreach] {datetime.utcnow():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _meta(key, default=None):
    with db.connect() as con:
        return db.get_meta(con, key, default)


def _set(key, value):
    with db.connect() as con:
        db.set_meta(con, key, value)


def prepare_batch(max_prospects=None) -> list:
    """Research + write first emails until today's quota of approved drafts is ready."""
    cfg = config.load()
    tgt = cfg["targeting"]
    results = []
    with db.connect() as con:
        quota = sequence.new_quota(con)
        ready = len(sequence.ready_first_emails(con, 100))
        need = max(0, quota - ready)
        rows = con.execute("SELECT * FROM prospects WHERE status='new'").fetchall()
    have = serp.researched_ids()
    rows = [r for r in rows if r["id"] in have]  # only prospects with uploaded live Google checks
    pr = {v: i for i, v in enumerate(tgt["priority_order"])}
    st = {v: i for i, v in enumerate(tgt["state_order"])}
    rows.sort(key=lambda r: (pr.get(r["priority"], 9), st.get((r["state"] or "")[:2], 9), r["id"]))
    budget = max_prospects if max_prospects is not None else need * 3
    # research is uploaded by hand; nothing else to pick from
    for r in rows[:budget]:
        if need <= 0:
            break
        try:
            res = writer.draft_email1(r["id"])
        except Exception as e:
            res = {"status": "error", "problems": [str(e)]}
            with db.connect() as con:
                db.log_event(con, r["id"], "prepare_error", str(e))
        results.append((r["id"], r["center"], res.get("status"), res.get("score")))
        _log(f"prepared #{r['id']} {r['center']}: {res.get('status')} score={res.get('score')}")
        if res.get("status") == "approved":
            need -= 1
    return results


def _in_window(now, lim):
    start = now.replace(hour=int(lim["window_start"][:2]), minute=int(lim["window_start"][3:]), second=0, microsecond=0)
    end = now.replace(hour=int(lim["window_end"][:2]), minute=int(lim["window_end"][3:]), second=0, microsecond=0)
    return start <= now <= end


def _send_thread():
    if not _send_lock.acquire(blocking=False):
        return
    try:
        sequence.run_send(wait=False, process_inbox=inbox.process_inbox)
    except Exception:
        _log("send failed:\n" + traceback.format_exc())
    finally:
        _send_lock.release()


def tick():
    if config.is_configured():
        return
    cfg = config.load()
    lim = cfg["limits"]
    now = sequence.now_et()
    today = now.date().isoformat()
    weekday = sequence.DAY_NAMES[now.weekday()]

    last_inbox = _meta("last_inbox_at")
    if not last_inbox or datetime.fromisoformat(last_inbox) < datetime.utcnow() - timedelta(minutes=15):
        _set("last_inbox_at", datetime.utcnow().isoformat())
        if not _send_lock.locked():
            try:
                n = inbox.process_inbox(verbose=False)
                if n:
                    _log(f"inbox: {n} replies handled")
            except Exception:
                _log("inbox failed:\n" + traceback.format_exc())

    if (weekday in lim["new_email_days"] and now.hour >= 6 and _meta("last_prepare_date") != today
            and _meta("templates_approved")):
        _set("last_prepare_date", today)
        with _work_lock:
            try:
                prepare_batch()
            except Exception:
                _log("prepare failed:\n" + traceback.format_exc())

    if weekday in set(lim["new_email_days"]) | set(lim["followup_days"]) and _in_window(now, lim):
        if not _send_lock.locked():
            threading.Thread(target=_send_thread, daemon=True).start()

    if now.hour * 60 + now.minute >= 12 * 60 + 30 and _meta("last_digest_date") != today and cfg.get("digest_to"):
        _set("last_digest_date", today)
        try:
            digest.send_digest()
        except Exception:
            _log("digest failed:\n" + traceback.format_exc())


def _loop():
    _log("scheduler started")
    while True:
        try:
            tick()
        except Exception:
            _log("tick failed:\n" + traceback.format_exc())
        time.sleep(TICK_SECONDS)


def start():
    global _started
    if _started or not config.ENABLED:
        return
    _started = True
    db.init()
    threading.Thread(target=_loop, daemon=True).start()
