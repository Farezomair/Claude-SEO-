"""Send + IMAP access for the outreach mailbox (Hostinger).

Sending goes direct over SMTP, or, where the host blocks outbound SMTP (Railway), through the site's
WordPress mail relay over HTTPS (OUTREACH_RELAY_USER + OUTREACH_RELAY_APP_PASSWORD, a WP Application Password).
"""
import base64
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

import httpx

from . import config


def build(to_addr, subject, body, in_reply_to=None, refs=None, attachment=None):
    cfg = config.load()
    s = cfg["sender"]
    msg = EmailMessage()
    msg["From"] = formataddr((s["name"], s["email"]))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=s["email"].split("@")[-1] or None)
    msg["List-Unsubscribe"] = f"<mailto:{s['email']}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (refs + " " if refs else "") + in_reply_to
    msg.set_content(body)
    if attachment:
        path = Path(attachment)
        ctype = mimetypes.guess_type(path.name)[0] or "application/pdf"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def _relay():
    """Relay settings, when sending goes through the site's WordPress (hosts that block outbound SMTP)."""
    user = os.getenv("OUTREACH_RELAY_USER", "").strip()
    pw = os.getenv("OUTREACH_RELAY_APP_PASSWORD", "").strip()
    url = os.getenv("OUTREACH_RELAY_URL", "").strip()
    if not (user and pw):
        return None
    if not url:
        site = (config.load()["company"].get("website") or "").strip().removeprefix("https://").strip("/")
        url = f"https://{site}/wp-json/br-relay/v1/send"
    return url, user, pw


def _send_via_relay(msg: EmailMessage, relay):
    url, user, pw = relay
    name, addr = parseaddr(msg["From"])
    payload = {
        "to": msg["To"], "subject": msg["Subject"], "from_email": addr, "from_name": name,
        "text": msg.get_body(preferencelist=("plain",)).get_content(),
        "message_id": msg["Message-ID"], "in_reply_to": msg.get("In-Reply-To") or "",
        "references": msg.get("References") or "", "list_unsubscribe": msg.get("List-Unsubscribe") or "",
    }
    for part in msg.iter_attachments():
        payload["attachment"] = {"filename": part.get_filename(), "content_type": part.get_content_type(),
                                 "content_b64": base64.b64encode(part.get_payload(decode=True)).decode()}
        break
    r = httpx.post(url, json=payload, auth=(user, pw), timeout=90)
    if r.status_code >= 300:
        raise RuntimeError(f"mail relay refused the send ({r.status_code}): {r.text[:300]}")


def send(msg: EmailMessage):
    cfg = config.load()["sender"]
    relay = _relay()
    if relay:
        _send_via_relay(msg, relay)
    else:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx, timeout=60) as smtp:
            smtp.login(cfg.get("login", cfg["email"]), config.mail_password())
            smtp.send_message(msg)
    try:
        save_to_sent(msg)
    except Exception as e:  # sending succeeded; a missing Sent copy is not fatal
        print(f"  warning: could not save copy to Sent folder: {e}")


def imap():
    cfg = config.load()["sender"]
    m = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    m.login(cfg.get("login", cfg["email"]), config.mail_password())
    return m


def sent_folder(m):
    cfg = config.load()["sender"]
    if cfg.get("sent_folder") and cfg["sent_folder"] != "auto":
        return cfg["sent_folder"]
    typ, data = m.list()
    names = []
    for line in data or []:
        line = line.decode(errors="replace")
        name = re.findall(r'"([^"]+)"\s*$|(\S+)\s*$', line)
        nm = (name[0][0] or name[0][1]) if name else ""
        if "\\Sent" in line:
            return nm
        names.append(nm)
    for cand in ("INBOX.Sent", "Sent", "Sent Items", "Sent Messages"):
        if cand in names:
            return cand
    return "INBOX.Sent"


def save_to_sent(msg: EmailMessage):
    m = imap()
    try:
        folder = sent_folder(m)
        m.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    finally:
        m.logout()
