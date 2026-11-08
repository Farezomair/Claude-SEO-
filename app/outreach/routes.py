"""Outreach page in Ascend: status, review queue, replies that need the owner, audit upload, controls."""
import json
import os
import tempfile
import threading
from collections import Counter

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import current_user
from . import config, db, digest, importer, inbox, mailer, scheduler, sequence

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


def _guard(request):
    return None if current_user(request) else RedirectResponse("/login", status_code=303)


def _back(msg=""):
    return RedirectResponse("/outreach" + (f"?msg={msg}" if msg else ""), status_code=303)


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
        "labels": STATUS_LABELS,
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


@router.get("/outreach/export-settings")
def export_settings(request: Request):
    if (r := _guard(request)):
        return r
    return JSONResponse(config.load())


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
