"""Decide what is due today and send it within the limits."""
import json
import random
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from . import config, db, mailer, rules, templates

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def tz():
    return ZoneInfo(config.load()["limits"]["timezone"])


def now_et():
    return datetime.now(tz())


def to_et(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(tz())


def sent_today(con) -> list:
    start = now_et().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC")).isoformat()
    return con.execute("SELECT * FROM messages WHERE direction='out' AND status='sent' AND sent_at>=?", (start,)).fetchall()


def new_quota(con) -> int:
    lim = config.load()["limits"]
    first = db.get_meta(con, "first_send_date")
    week = 1 if not first else (now_et().date() - date.fromisoformat(first)).days // 7 + 1
    quota = lim["ramp"][-1]["new_per_day"]
    for step in lim["ramp"]:
        if week <= step["week"]:
            quota = step["new_per_day"]
            break
    done = sum(1 for m in sent_today(con) if m["kind"] == "email1")
    return max(0, quota - done)


def total_left(con) -> int:
    return max(0, config.load()["limits"]["max_total_per_day"] - len(sent_today(con)))


def paused(con):
    """Manual pause (Pause button) stays until Resume. An automatic bounce pause only lasts until the next day."""
    manual = db.get_meta(con, "paused")
    if manual:
        return manual
    until = db.get_meta(con, "auto_paused_until")
    if until and now_et().date().isoformat() < until:
        return db.get_meta(con, "auto_paused_reason") or "automatic pause"
    return None


def queue_status(con) -> dict:
    """How many send days the ready first emails cover."""
    lim = config.load()["limits"]
    ready = len(ready_first_emails(con, 1000))
    per_day = lim["ramp"][0]["new_per_day"] if lim.get("ramp") else 2
    return {"ready": ready, "send_days_covered": ready // max(1, per_day), "per_day": per_day}


def thread_of(con, prospect_id):
    """Message-IDs in this prospect's thread, oldest first (outbound and inbound)."""
    rows = con.execute("SELECT message_id FROM messages WHERE prospect_id=? AND message_id IS NOT NULL ORDER BY id",
                       (prospect_id,)).fetchall()
    return [r["message_id"] for r in rows]


def name_for(p):
    return p["first_name"] or "there"


def slots_for(con, p, **extra):
    s = templates.base_slots(config.load())
    first = con.execute("SELECT subject FROM messages WHERE prospect_id=? AND kind='email1' ORDER BY id LIMIT 1",
                        (p["id"],)).fetchone()
    s.update(name=name_for(p), center=p["center"],
             thread_subject=(first["subject"] if first else f"{p['center']} on Google").removeprefix("Re: "))
    s.update(extra)
    return s


def send_message(con, p, kind, subject, body, attachment=None, dry_run=False, to_addr=None, review_notes=None):
    """Send one message in the prospect's thread and record it. Returns True if sent."""
    problems = rules.check(kind, subject, body, {"name": name_for(p)})
    if problems:
        con.execute("""INSERT INTO messages(prospect_id,kind,direction,status,to_addr,subject,body,review_notes,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (p["id"], kind, "out", "needs_review", to_addr or p["email"], subject, body,
                     json.dumps({"problems": problems}), db.now_iso()))
        db.log_event(con, p["id"], "blocked", f"{kind}: {problems}")
        return False
    chain = thread_of(con, p["id"])
    msg = mailer.build(to_addr or p["email"], subject, body,
                       in_reply_to=chain[-1] if chain and kind != "email1" else None,
                       refs=" ".join(chain[:-1]) if len(chain) > 1 and kind != "email1" else None,
                       attachment=attachment)
    if dry_run:
        print(f"  [dry run] would send {kind} to {msg['To']}: {subject}")
        return False
    mailer.send(msg)
    con.execute("""INSERT INTO messages(prospect_id,kind,direction,status,to_addr,from_addr,subject,body,attachment,
                   message_id,in_reply_to,refs,review_notes,created_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["id"], kind, "out", "sent", msg["To"], config.load()["sender"]["email"], subject, body,
                 str(attachment) if attachment else None, msg["Message-ID"], msg.get("In-Reply-To"),
                 msg.get("References"), review_notes, db.now_iso(), db.now_iso()))
    if kind == "email1" and not db.get_meta(con, "first_send_date"):
        db.set_meta(con, "first_send_date", now_et().date().isoformat())
    con.commit()
    return True


def first_sent_at(con, pid, kind):
    r = con.execute("SELECT sent_at FROM messages WHERE prospect_id=? AND kind=? AND status='sent' ORDER BY id LIMIT 1",
                    (pid, kind)).fetchone()
    return to_et(r["sent_at"]) if r else None


def due_followups(con):
    seq = config.load()["sequence"]
    today = now_et().date()
    due = []
    rules_ = [
        ("email1_sent", "email1", seq["followup_1_after_days"], "fu1", "fu1_sent"),
        ("fu1_sent", "email1", seq["followup_2_after_days"], "fu2", "sequence_finished"),
        ("audit_sent", "audit", seq["post_audit_followup_days"][0], "pa_fu1", "pa_fu1_sent"),
        ("pa_fu1_sent", "audit", seq["post_audit_followup_days"][1], "pa_fu2", "handed_off"),
    ]
    for status, anchor, days, kind, next_status in rules_:
        for p in con.execute("SELECT * FROM prospects WHERE status=?", (status,)).fetchall():
            t0 = first_sent_at(con, p["id"], anchor)
            if t0 and (today - t0.date()).days >= days:
                due.append((p, kind, next_status))
    # paused for out-of-office: resume when the date passes
    for p in con.execute("SELECT * FROM prospects WHERE status='ooo' AND next_action_at IS NOT NULL").fetchall():
        if date.fromisoformat(p["next_action_at"][:10]) <= today:
            prev = (p["notes"] or "").split("resume:")[-1].split("|")[0].strip() or "email1_sent"
            db.set_status(con, p["id"], prev, note="back from out of office")
    return due


def ready_first_emails(con, limit):
    cfg = config.load()
    tgt = cfg["targeting"]
    need = "('draft','approved')" if cfg.get("auto_send") else "('approved')"
    rows = con.execute(f"""SELECT p.*, m.id AS mid, m.subject AS m_subject, m.body AS m_body, m.review_notes AS m_notes
        FROM prospects p JOIN messages m ON m.prospect_id=p.id
        WHERE m.kind='email1' AND m.direction='out' AND m.status IN {need}
          AND p.status IN ('drafted','approved')""").fetchall()
    pr = {v: i for i, v in enumerate(tgt["priority_order"])}
    st = {v: i for i, v in enumerate(tgt["state_order"])}
    rows = sorted(rows, key=lambda r: (pr.get(r["priority"], 9), st.get((r["state"] or "")[:2], 9), r["id"]))
    return rows[:limit]


def run_send(dry_run=False, wait=True, process_inbox=None):
    cfg = config.load()
    lim = cfg["limits"]
    with db.connect() as con:
        if not db.get_meta(con, "templates_approved"):
            print("Templates not approved yet (Outreach page in Ascend).")
            return
        if paused(con):
            print(f"Sending is paused: {paused(con)}.")
            return

    now = now_et()
    start = now.replace(hour=int(lim["window_start"][:2]), minute=int(lim["window_start"][3:]), second=0)
    end = now.replace(hour=int(lim["window_end"][:2]), minute=int(lim["window_end"][3:]), second=0)
    if now < start:
        if not wait or (start - now) > timedelta(hours=3):
            print(f"Outside the send window ({lim['window_start']} to {lim['window_end']} ET).")
            return
        print(f"Waiting until {lim['window_start']} ET...")
        time.sleep((start - now).total_seconds())
    if now_et() > end and not dry_run:
        print("Send window is over for today.")
        return

    dayname = DAY_NAMES[now_et().weekday()]
    if now_et().date().isoformat() in (lim.get("skip_dates") or []):
        print("Holiday / skip date: no cold emails today; due follow-ups go out on the next send day.")
        return
    if process_inbox and not dry_run:
        process_inbox()

    with db.connect() as con:
        queue = []
        if dayname in lim["followup_days"]:
            for p, kind, next_status in due_followups(con):
                queue.append(("fu", p, kind, next_status))
        if dayname in lim["new_email_days"]:
            for r in ready_first_emails(con, new_quota(con)):
                queue.append(("new", r, "email1", "email1_sent"))
        queue = queue[:total_left(con)]
    print(f"{len(queue)} emails due ({sum(1 for q in queue if q[0]=='fu')} follow-ups, "
          f"{sum(1 for q in queue if q[0]=='new')} first emails).")

    sent = 0
    for i, (src, p, kind, next_status) in enumerate(queue):
        if now_et() > end and not dry_run:
            print("Window closed; the rest wait for the next send day.")
            break
        if process_inbox and not dry_run and i and i % 5 == 0:
            process_inbox()
        with db.connect() as con:
            if paused(con):
                print(f"Paused: {paused(con)}")
                break
            fresh = con.execute("SELECT * FROM prospects WHERE id=?", (p["id"],)).fetchone()
            expected = {"fu1": "email1_sent", "fu2": "fu1_sent", "pa_fu1": "audit_sent", "pa_fu2": "pa_fu1_sent",
                        "email1": None}[kind]
            if expected and fresh["status"] != expected:
                continue  # they replied or something changed since we queued
            if kind == "email1":
                if fresh["status"] not in ("drafted", "approved"):
                    continue
                subject, body = p["m_subject"], p["m_body"]
            else:
                extra = {}
                if kind == "fu1":
                    notes = con.execute("SELECT review_notes FROM messages WHERE prospect_id=? AND kind='email1' AND status='sent'",
                                        (p["id"],)).fetchone()
                    extra["fu1_line"] = json.loads(notes["review_notes"] or "{}").get("fields", {}).get("fu1_line", "").strip() if notes else ""
                    if not extra["fu1_line"]:
                        extra["fu1_line"] = "The first thing I'd fix is the page families land on first."
                subject, body = templates.render(kind, **slots_for(con, fresh, **extra))
            ok = send_message(con, fresh, kind, subject, body, dry_run=dry_run,
                              review_notes=p["m_notes"] if kind == "email1" else None)
            if ok:
                if kind == "email1":
                    con.execute("DELETE FROM messages WHERE id=?", (p["mid"],))
                db.set_status(con, p["id"], next_status)
                sent += 1
                print(f"  sent {kind} to {fresh['center']} <{fresh['email']}>")
        if i < len(queue) - 1 and not dry_run:
            time.sleep(random.randint(lim["min_gap_seconds"], lim["max_gap_seconds"]))
    print(f"Done: {sent} sent.")
