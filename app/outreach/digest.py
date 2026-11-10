"""Daily lead digest emailed to the owner, plus the audit hand-off."""
from collections import Counter
from pathlib import Path

from . import config, db, mailer, sequence

FUNNEL = [
    ("audit_requested", "Said yes, audit to build"),
    ("needs_owner", "Waiting on your reply"),
    ("handed_off", "Handed to you (call or post-audit reply)"),
    ("audit_sent", "Audit sent, follow-ups running"),
    ("pa_fu1_sent", "Audit sent, 1 follow-up done"),
    ("email1_sent", "First email sent"),
    ("fu1_sent", "Follow-up 1 sent"),
    ("sequence_finished", "No reply, sequence finished"),
    ("later", "Not now (recontact later)"),
    ("ooo", "Out of office (paused)"),
    ("dnc", "Do not contact"),
    ("bounced", "Bounced"),
    ("drafted", "Drafted, ready to send"),
    ("needs_review", "Draft needs your review"),
    ("skipped", "Skipped (not a fit)"),
    ("new", "Not researched yet"),
]


def build_digest():
    with db.connect() as con:
        counts = Counter(r["status"] for r in con.execute("SELECT status FROM prospects"))
        today = sequence.sent_today(con)
        start = sequence.now_et().replace(hour=0, minute=0, second=0).astimezone(sequence.ZoneInfo("UTC")).isoformat()
        replies = con.execute("""SELECT p.center, p.status, m.classification FROM messages m JOIN prospects p ON p.id=m.prospect_id
                                 WHERE m.direction='in' AND m.created_at>=? ORDER BY m.id""", (start,)).fetchall()
        hot = con.execute("""SELECT id, center, website, status, notes FROM prospects
                             WHERE status IN ('audit_requested','needs_owner','handed_off') ORDER BY updated_at DESC""").fetchall()
        paused = sequence.paused(con)
    import json
    lines = [f"Outreach, {sequence.now_et():%A %b %d, %Y}", ""]
    if paused:
        lines += [f"SENDING IS PAUSED: {paused}", ""]
    with db.connect() as con:
        q = sequence.queue_status(con)
    from . import health
    h = health.check()
    lines += [f"Queue: {q['ready']} first emails ready, about {q['send_days_covered']} send days."]
    if q["send_days_covered"] < 3:
        lines += ["WARNING: the queue is low. Run the weekly research task (Scheduled in the Claude app) so sending doesn't stop."]
    if not h["relay_ok"]:
        lines += [f"WARNING: mail relay check failed: {h['relay_detail']}"]
    lines += [""]
    kinds = Counter(m["kind"] for m in today)
    lines += [f"Sent today: {len(today)} (first emails {kinds.get('email1', 0)}, follow-ups "
              f"{kinds.get('fu1', 0) + kinds.get('fu2', 0)}, replies {sum(v for k, v in kinds.items() if k not in ('email1', 'fu1', 'fu2'))})",
              f"Replies today: {len(replies)}", ""]
    if hot:
        lines.append("Needs you:")
        for h in hot:
            last_note = (h["notes"] or "").split(" | ")[-1]
            lines.append(f"  #{h['id']} {h['center']} ({h['website']}): {h['status'].replace('_', ' ')}. {last_note}")
        lines.append("")
    if replies:
        lines.append("Replies today:")
        for r in replies:
            c = json.loads(r["classification"] or "{}")
            lines.append(f"  {r['center']}: {c.get('category', '?')}. {c.get('summary', '')}")
        lines.append("")
    lines.append("Funnel:")
    for status, label in FUNNEL:
        if counts.get(status):
            lines.append(f"  {counts[status]:>4}  {label}")
    return "\n".join(lines)


def send_digest():
    cfg = config.load()
    body = build_digest()
    msg = mailer.build(cfg["digest_to"], f"Outreach digest, {sequence.now_et():%b %d}", body)
    mailer.send(msg)
    return body


def send_audit(prospect_id: int, pdf: str, first_fix: str, dry_run=False):
    path = Path(pdf)
    if not path.exists():
        raise SystemExit(f"PDF not found: {pdf}")
    first_fix = first_fix.strip()
    if not first_fix.endswith("."):
        first_fix += "."
    with db.connect() as con:
        p = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not p:
            raise SystemExit(f"No prospect #{prospect_id}")
        if p["status"] != "audit_requested":
            print(f"Note: #{prospect_id} status is {p['status']}, not audit_requested. Sending anyway.")
        from . import templates
        subject, body = templates.render("audit", **sequence.slots_for(con, p, first_fix=first_fix))
        print(body)
        if sequence.send_message(con, p, "audit", subject, body, attachment=path, dry_run=dry_run):
            if not dry_run:
                db.set_status(con, prospect_id, "audit_sent", note=f"audit: {path.name}")
            print(f"Audit sent to {p['email']}.")
            return True
        return False
