"""Outreach page in Ascend: status, review queue, replies that need the owner, audit upload, controls."""
import json
import os
import tempfile
import threading
from collections import Counter
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import current_user
from . import config, db, digest, importer, inbox, mailer, rules, scheduler, sequence, serp
from . import templates as tmpl

router = APIRouter()
templates = None  # set by main.py so the page shares Ascend's Jinja environment and filters

STATUS_LABELS = {
    "audit_requested": "Said yes: audit to build", "needs_owner": "Waiting on your reply",
    "handed_off": "Handed to you", "audit_sent": "Audit sent", "pa_fu1_sent": "Audit sent, followed up",
    "email1_sent": "First email sent", "fu1_sent": "Follow-up 1 sent", "sequence_finished": "No reply, finished",
    "later": "Not now", "ooo": "Out of office", "dnc": "Do not contact", "bounced": "Bounced",
    "approved": "Ready to send", "needs_review": "Draft needs your review", "skipped": "Skipped",
    "new": "Not researched yet",
}


def _health_safe():
    try:
        from . import health
        return health.check()
    except Exception as e:
        return {"healthy": False, "relay_ok": False, "relay_detail": str(e), "ready": 0, "send_days_covered": 0}


def _guard(request):
    return None if current_user(request) else RedirectResponse("/login", status_code=303)


def _back(msg=""):
    return RedirectResponse("/outreach" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@router.get("/outreach", response_class=HTMLResponse)
def outreach_page(request: Request, msg: str = ""):
    if (r := _guard(request)):
        return r
    cfg = config.load()
    missing = config.is_configured(cfg)
    with db.connect() as con:
        counts = Counter(r["status"] for r in con.execute("SELECT status FROM prospects").fetchall())
        review = con.execute("""SELECT p.id, p.center, p.website, p.email, m.id AS mid, m.subject, m.body, m.review_notes
                                FROM messages m JOIN prospects p ON p.id=m.prospect_id
                                WHERE m.kind='email1' AND m.status='needs_review' ORDER BY p.id""").fetchall()
        ready = con.execute("""SELECT p.id, p.center, p.email, m.subject, m.body, m.review_notes
                               FROM messages m JOIN prospects p ON p.id=m.prospect_id
                               WHERE m.kind='email1' AND m.status='approved' ORDER BY p.id""").fetchall()
        needs_you = con.execute("""SELECT id, center, website, email, status, notes, updated_at FROM prospects
                                   WHERE status IN ('audit_requested','needs_owner','handed_off') ORDER BY updated_at DESC""").fetchall()
        replies = con.execute("""SELECT p.center, m.from_addr, m.subject, m.body, m.classification, m.created_at
                                 FROM messages m JOIN prospects p ON p.id=m.prospect_id
                                 WHERE m.direction='in' ORDER BY m.id DESC LIMIT 15""").fetchall()
        sent = con.execute("""SELECT p.center, m.kind, m.to_addr, m.subject, m.sent_at FROM messages m
                              JOIN prospects p ON p.id=m.prospect_id WHERE m.direction='out' AND m.status='sent'
                              ORDER BY m.id DESC LIMIT 20""").fetchall()
        events = con.execute("SELECT prospect_id, at, event, detail FROM events ORDER BY id DESC LIMIT 25").fetchall()
        paused = sequence.paused(con)
        approved_tpl = db.get_meta(con, "templates_approved")
        first_send = db.get_meta(con, "first_send_date")
    for row in review + ready:
        notes = json.loads(row.get("review_notes") or "{}")
        row["score"] = (notes.get("review") or {}).get("score")
        row["fixes"] = (notes.get("review") or {}).get("fixes", [])
        row["problems"] = notes.get("problems", [])
    for row in replies:
        row["c"] = json.loads(row.get("classification") or "{}")
    funnel = [(STATUS_LABELS.get(k, k), v) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return templates.TemplateResponse("outreach.html", {
        "request": request, "user": current_user(request), "nav": "outreach", "msg": msg,
        "enabled": config.ENABLED, "missing": missing, "paused": paused, "approved_tpl": approved_tpl,
        "first_send": first_send, "funnel": funnel, "total": sum(counts.values()), "review": review,
        "ready": ready, "needs_you": needs_you, "replies": replies, "sent": sent, "events": events,
        "sender": cfg["sender"], "company": cfg["company"], "limits": cfg["limits"],
        "labels": STATUS_LABELS, "research_next": research_queue(8), "health": _health_safe(),
    })


@router.post("/outreach/upload-prospects")
async def upload_prospects(request: Request, file: UploadFile = File(...)):
    if (r := _guard(request)):
        return r
    added, skipped = importer.import_prospects_xlsx(await file.read())
    return _back(f"Imported {added} prospects ({skipped} rows had no usable email).")


@router.post("/outreach/upload-state")
async def upload_state(request: Request, file: UploadFile = File(...)):
    if (r := _guard(request)):
        return r
    counts = importer.import_state(await file.read())
    return _back("State imported: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def research_queue(limit=10):
    """Next prospects to research by hand: qualified, not yet researched, in priority order."""
    cfg = config.load()
    tgt = cfg["targeting"]
    have = serp.researched_ids()
    with db.connect() as con:
        rows = con.execute("SELECT id, center, website, state, cities, levels_of_care, priority, website_check, mx_ok, email "
                           "FROM prospects WHERE status='new'").fetchall()
    rows = [r for r in rows if r["id"] not in have and r["website_check"] in tgt["website_check"]
            and (not tgt["require_mx_ok"] or r["mx_ok"] == "Yes")]
    pr = {v: i for i, v in enumerate(tgt["priority_order"])}
    st = {v: i for i, v in enumerate(tgt["state_order"])}
    rows.sort(key=lambda r: (pr.get(r["priority"], 9), st.get((r["state"] or "")[:2], 9), r["id"]))
    return rows[:limit]


@router.get("/outreach/status")
def status_json(request: Request):
    """Health + queue for the weekly research task: is anything going to stop email?"""
    if (r := _guard(request)):
        return r
    from . import health
    return JSONResponse(health.check())


@router.get("/outreach/research-queue")
def research_queue_json(request: Request):
    if (r := _guard(request)):
        return r
    return JSONResponse(research_queue(20))


@router.post("/outreach/upload-research")
async def upload_research(request: Request, file: UploadFile = File(...)):
    """Research file from a Claude session: live Google checks per prospect, optionally a finished email.

    {"prospects": [{"id": 6, "checks": [{query, location, maps_top, prospect_in_maps, organic_top,
      prospect_organic, notes}], "first_name": "Eric", "first_name_source": "...",
      "email": {"subject": "...", "core": "<paragraphs between greeting and signature>", "fu1_line": "..."}}]}
    """
    if (r := _guard(request)):
        return r
    data = json.loads((await file.read()).decode("utf8"))
    cfg = config.load()
    done = []
    for item in data.get("prospects", []):
        pid = int(item["id"])
        with db.connect() as con:
            p = con.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not p:
            done.append(f"#{pid} not found")
            continue
        if item.get("skip"):
            with db.connect() as con:
                db.set_status(con, pid, "skipped", note="not a fit: " + str(item["skip"])[:300])
            done.append(f"#{pid} skipped")
            continue
        if item.get("checks"):
            serp.add(pid, item["checks"])
        if "first_name" in item:
            with db.connect() as con:
                con.execute("UPDATE prospects SET first_name=?, first_name_source=? WHERE id=?",
                            (item.get("first_name") or None, item.get("first_name_source"), pid))
        em = item.get("email")
        if not em:
            done.append(f"#{pid} research saved")
            continue
        slots = tmpl.base_slots(cfg)
        slots.update(name=item.get("first_name") or p["first_name"] or "there")
        body = f"Hi {slots['name']},\n\n{em['core'].strip()}\n\n" + tmpl.FOOTER.format(**slots)
        problems = rules.check("email1", em["subject"], body, slots)
        status = "approved" if not problems else "needs_review"
        notes = {"problems": problems, "source": "written in a Claude session from live Google checks",
                 "fields": {"fu1_line": em.get("fu1_line", "")}, "review": {"score": em.get("score")}}
        with db.connect() as con:
            con.execute("DELETE FROM messages WHERE prospect_id=? AND kind='email1' AND status IN ('draft','needs_review','approved')", (pid,))
            con.execute("""INSERT INTO messages(prospect_id,kind,direction,status,to_addr,from_addr,subject,body,review_notes,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (pid, "email1", "out", status, p["email"], cfg["sender"]["email"], em["subject"], body,
                         json.dumps(notes), db.now_iso()))
            db.set_status(con, pid, status, note="email uploaded from research session")
        done.append(f"#{pid} email {status}" + (f" ({'; '.join(problems)})" if problems else ""))
    return _back("Research uploaded: " + ", ".join(done))


@router.get("/outreach/export-settings")
def export_settings(request: Request):
    if (r := _guard(request)):
        return r
    return JSONResponse(config.load())


@router.post("/outreach/upload-settings")
async def upload_settings(request: Request, file: UploadFile = File(...)):
    """Merge a partial settings file into the stored settings (e.g. {"limits": {"skip_dates": [...]}})."""
    if (r := _guard(request)):
        return r
    patch = json.loads((await file.read()).decode("utf8"))
    with db.connect() as con:
        raw = db.get_meta(con, "settings")
    current = json.loads(raw) if raw else {}
    config.save(config._merge(current, patch))
    return _back("Settings updated: " + ", ".join(patch.keys()))


@router.post("/outreach/approve/{pid}")
def approve(request: Request, pid: int, subject: str = Form(None), body: str = Form(None)):
    if (r := _guard(request)):
        return r
    with db.connect() as con:
        if subject and body:
            con.execute("UPDATE messages SET subject=?, body=? WHERE prospect_id=? AND kind='email1' AND status='needs_review'",
                        (subject.strip(), body.replace("\r\n", "\n").strip(), pid))
        con.execute("UPDATE messages SET status='approved' WHERE prospect_id=? AND kind='email1' AND status='needs_review'", (pid,))
        db.set_status(con, pid, "approved", note="approved in Ascend")
    return _back(f"#{pid} approved.")


@router.post("/outreach/reject/{pid}")
def reject(request: Request, pid: int):
    if (r := _guard(request)):
        return r
    with db.connect() as con:
        con.execute("DELETE FROM messages WHERE prospect_id=? AND kind='email1' AND status!='sent'", (pid,))
        db.set_status(con, pid, "skipped", note="rejected in Ascend")
    return _back(f"#{pid} skipped.")


@router.post("/outreach/mark/{pid}")
def mark(request: Request, pid: int, status: str = Form(...)):
    if (r := _guard(request)):
        return r
    with db.connect() as con:
        db.set_status(con, pid, status, note="set in Ascend")
    return _back(f"#{pid} marked {status}.")


@router.post("/outreach/audit/{pid}")
async def send_audit(request: Request, pid: int, first_fix: str = Form(...), file: UploadFile = File(...)):
    if (r := _guard(request)):
        return r
    name = os.path.basename(file.filename or "audit.pdf")
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, "wb") as f:
        f.write(await file.read())
    digest.send_audit(pid, path, first_fix)
    return _back(f"Audit sent to #{pid}.")


@router.post("/outreach/control/{action}")
def control(request: Request, action: str):
    if (r := _guard(request)):
        return r
    with db.connect() as con:
        if action == "pause":
            db.set_meta(con, "paused", "paused in Ascend")
        elif action == "resume":
            db.del_meta(con, "paused")
            db.del_meta(con, "auto_paused_until")
        elif action == "approve-templates":
            db.set_meta(con, "templates_approved", db.now_iso())
    if action == "inbox":
        threading.Thread(target=inbox.process_inbox, daemon=True).start()
    elif action == "prepare":
        threading.Thread(target=scheduler.prepare_batch, daemon=True).start()
    elif action == "digest":
        threading.Thread(target=digest.send_digest, daemon=True).start()
    elif action == "test-mail":
        cfg = config.load()
        m = mailer.build(cfg["digest_to"], "Outreach test from Ascend", "If you can read this, the cloud agent can send mail.")
        mailer.send(m)
    return _back(f"{action} done.")
