"""Read new mail, match it to prospects, sort the reply and act on it the same day."""
import email
import json
import re
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from typing import Literal

from pydantic import BaseModel, Field

from . import config, db, llm, mailer, sequence, templates

ACTIVE = ("email1_sent", "fu1_sent", "sequence_finished", "audit_requested", "audit_sent", "pa_fu1_sent",
          "handed_off", "ooo", "later", "needs_owner", "replied")


class ReplyClass(BaseModel):
    category: Literal["interested", "question", "not_now", "unsubscribe", "wrong_person", "out_of_office",
                      "auto_reply", "call_request", "other"] = Field(
        description="interested = yes / send it / sounds good; call_request = wants to talk or book a call; "
                    "question = asks something (price, process, who we are); not_now = maybe later; "
                    "unsubscribe = no / remove me / not interested / stop; wrong_person = not them, maybe names who to ask; "
                    "out_of_office = away message; auto_reply = ticket confirmation or other automatic message; "
                    "other = anything else")
    summary: str = Field(description="one plain sentence on what they said")
    return_date: str = Field(description="for out_of_office: return date as YYYY-MM-DD if stated, else empty")
    referred_name: str = Field(description="for wrong_person: the name they point to, else empty")
    referred_email: str = Field(description="for wrong_person: the email they point to, else empty")
    contains_patient_info: bool = Field(description="true if the message includes any information about a patient or a person's health or treatment")


SYSTEM = """You sort replies to cold emails from a small SEO agency for addiction treatment centers. The emails offered a free 1-page review of how the center shows up on Google. Classify the reply. Quoted text from our own earlier emails is not part of their reply; judge only what they wrote."""


def _dec(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf8", errors="replace")


def strip_quoted(body):
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*>", line) or re.match(r"^\s*On .+wrote:\s*$", line) or line.strip().startswith("-----Original Message"):
            break
        if re.match(r"^\s*From:\s", line) and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()[:4000]


def match_prospect(con, msg, from_addr):
    ids = re.findall(r"<[^>]+>", (msg.get("In-Reply-To") or "") + " " + (msg.get("References") or ""))
    for mid in reversed(ids):
        r = con.execute("SELECT prospect_id FROM messages WHERE message_id=?", (mid,)).fetchone()
        if r:
            return con.execute("SELECT * FROM prospects WHERE id=?", (r["prospect_id"],)).fetchone()
    r = con.execute("SELECT * FROM prospects WHERE email=? OR other_emails LIKE ?", (from_addr, f"%{from_addr}%")).fetchone()
    if r:
        return r
    dom = from_addr.split("@")[-1]
    if dom and dom not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com"):
        return con.execute(f"SELECT * FROM prospects WHERE domain=? AND status IN {ACTIVE}", (dom,)).fetchone()
    return None


def is_bounce(msg, from_addr):
    return (re.search(r"mailer-daemon|postmaster|mail delivery", from_addr + " " + _dec(msg.get("From")), re.I)
            or msg.get_content_type() == "multipart/report")


def bounced_recipient(con, msg):
    raw = msg.as_string()
    for m in re.findall(r"(?im)^(?:final|original)-recipient:\s*rfc822;\s*(\S+)", raw):
        return m.strip("<>").lower()
    for row in con.execute("SELECT email FROM prospects WHERE status IN ('email1_sent','fu1_sent','sequence_finished')"):
        if row["email"] and row["email"] in raw.lower():
            return row["email"]
    return None


def alert(subject, body):
    cfg = config.load()
    try:
        m = mailer.build(cfg["alerts_to"], "[Outreach agent] " + subject, body)
        mailer.send(m)
    except Exception as e:
        print(f"  could not send alert: {e}")


def reply_with(con, p, kind, **extra):
    """Send a templated reply right away (replies are not held for the morning window)."""
    if not db.get_meta(con, "templates_approved") or sequence.paused(con) or sequence.total_left(con) <= 0:
        db.log_event(con, p["id"], "reply_held", kind)
        return False
    subject, body = templates.render(kind, **sequence.slots_for(con, p, **extra))
    return sequence.send_message(con, p, kind, subject, body)


def check_bounce_rate(con):
    lim = config.load()["limits"]
    sent = len(sequence.sent_today(con))
    start = sequence.now_et().replace(hour=0, minute=0, second=0).astimezone(sequence.ZoneInfo("UTC")).isoformat()
    bounces = con.execute("SELECT COUNT(*) c FROM events WHERE event='bounce' AND at>=?", (start,)).fetchone()["c"]
    if bounces and sent >= lim["bounce_min_sample"] and bounces / sent > lim["bounce_pause_rate"] and not sequence.paused(con):
        db.set_meta(con, "paused", f"{bounces} bounces out of {sent} sent today")
        alert("Sending paused: bounce rate", f"{bounces} of {sent} emails bounced today. Sending is paused.\n"
              "Check the list, then press Resume on the Outreach page in Ascend.")


def process_inbox(verbose=True):
    m = mailer.imap()
    handled = 0
    try:
        m.select("INBOX")
        typ, data = m.uid("search", None, "ALL")
        uids = [int(u) for u in (data[0] or b"").split()]
        with db.connect() as con:
            last = db.get_meta(con, "last_uid")
            if last is None:  # first run: ignore everything already in the inbox (warm-up mail)
                db.set_meta(con, "last_uid", max(uids) if uids else 0)
                if verbose:
                    print(f"Inbox baseline set at {len(uids)} existing messages; only new mail is processed from now on.")
                return 0
            new = [u for u in uids if u > int(last)]
        for uid in new:
            typ, d = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            if not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            from_addr = parseaddr(msg.get("From", ""))[1].lower()
            with db.connect() as con:
                db.set_meta(con, "last_uid", uid)
                if from_addr in (config.load()["sender"]["email"], config.load()["sender"].get("login")):
                    continue
                if is_bounce(msg, from_addr):
                    rcpt = bounced_recipient(con, msg)
                    p = con.execute("SELECT * FROM prospects WHERE email=?", (rcpt,)).fetchone() if rcpt else None
                    if p:
                        db.log_event(con, p["id"], "bounce", rcpt)
                        db.set_status(con, p["id"], "bounced", note=f"bounced: {rcpt}")
                        if verbose:
                            print(f"  bounce: {p['center']} <{rcpt}>")
                        check_bounce_rate(con)
                    continue
                p = match_prospect(con, msg, from_addr)
                if not p:
                    continue
                handled += 1
                handle_reply(con, p, msg, from_addr, verbose)
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return handled


def handle_reply(con, p, msg, from_addr, verbose):
    cfg = config.load()
    body = strip_quoted(_text(msg))
    subject = _dec(msg.get("Subject"))
    try:
        c = llm.structured(SYSTEM, f"Their status with us: {p['status']}\nSubject: {subject}\n\nTheir reply:\n{body}",
                           ReplyClass, effort="low", max_tokens=4000)
    except RuntimeError as e:
        c = ReplyClass(category="other", summary=f"(could not classify: {e})", return_date="", referred_name="",
                       referred_email="", contains_patient_info=False)
    stored = "[removed: may contain patient information]" if c.contains_patient_info else body
    con.execute("""INSERT INTO messages(prospect_id,kind,direction,status,to_addr,from_addr,subject,body,message_id,
                   in_reply_to,classification,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["id"], "inbound", "in", "received", cfg["sender"]["email"], from_addr, subject, stored,
                 msg.get("Message-ID"), msg.get("In-Reply-To"), json.dumps(c.model_dump()), db.now_iso()))
    if verbose:
        print(f"  reply from {p['center']}: {c.category} ({c.summary})")
    if c.contains_patient_info:
        alert(f"Patient info received from {p['center']}",
              "A reply may contain patient information. The agent did not store it. "
              "Delete that email from the mailbox. Do not forward it.")

    after_audit = p["status"] in ("audit_sent", "pa_fu1_sent", "handed_off")
    cat = c.category
    if cat == "auto_reply":
        return
    if cat == "unsubscribe":
        reply_with(con, p, "optout_ack")
        db.set_status(con, p["id"], "dnc", note="opted out")
        return
    if cat == "out_of_office":
        back = c.return_date or (sequence.now_et().date() + timedelta(days=7)).isoformat()
        db.set_status(con, p["id"], "ooo", next_action_at=back, note=f"resume: {p['status']}")
        return
    if cat == "not_now":
        reply_with(con, p, "later_ack")
        later = (sequence.now_et().date() + timedelta(days=cfg["sequence"]["later_recontact_days"])).isoformat()
        db.set_status(con, p["id"], "later", next_action_at=later, note="not now")
        return
    if cat == "interested" and not after_audit and p["status"] != "audit_requested":
        reply_with(con, p, "ack")
        db.set_status(con, p["id"], "audit_requested")
        alert(f"Lead: {p['center']} wants the review",
              f"{p['center']} ({p['website']}) said yes.\n\nThey wrote: {c.summary}\n\n"
              f"The agent already replied that the review comes within 2 business days.\n"
              f"Next: build the Admissions Leak Audit, then run:\n"
              f"Ask Claude to build the audit, then upload the PDF on the Outreach page in Ascend (prospect #{p['id']}).")
        return
    # Questions, call requests, referrals, anything after the audit, anything unclear: the owner takes it.
    status = "handed_off" if after_audit or cat == "call_request" else "needs_owner"
    db.set_status(con, p["id"], status, note=f"{cat}: {c.summary}")
    extra = ""
    if cat == "wrong_person" and (c.referred_email or c.referred_name):
        extra = f"\nThey pointed to: {c.referred_name} {c.referred_email}".rstrip()
    alert(f"{p['center']} replied ({cat.replace('_', ' ')})",
          f"{p['center']} ({p['website']}) replied.\n\nSummary: {c.summary}{extra}\n\n"
          f"Their message:\n{stored}\n\nReply to them yourself as {cfg['sender']['name']}, from {cfg['sender']['email']}. "
          f"The sequence for this center has stopped.")
