"""SMTP send + IMAP access for the outreach mailbox (Hostinger)."""
import imaplib
import mimetypes
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

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


def send(msg: EmailMessage):
    cfg = config.load()["sender"]
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
