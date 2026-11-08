"""Outreach settings.

Business-specific settings (sender persona, company, mailing address, writer examples)
are stored in the database (outreach_meta 'settings'), uploaded through the Outreach page,
so they never live in this public code. Secrets come from environment variables:

  OUTREACH_ENABLED=true        turn the scheduler on
  OUTREACH_MAIL_PASSWORD       mailbox password (SMTP + IMAP)
  ANTHROPIC_API_KEY            already used by Ascend
"""
import copy
import json
import os

DEFAULTS = {
    "sender": {"name": "", "first_name": "", "email": "", "login": "",
               "smtp_host": "smtp.hostinger.com", "smtp_port": 465,
               "imap_host": "imap.hostinger.com", "imap_port": 993, "sent_folder": "auto"},
    "company": {"name": "", "tagline": "", "website": "", "mailing_address": "", "calendly": ""},
    "digest_to": "",
    "alerts_to": "",
    "model": "claude-opus-5-5",
    "auto_send_min_score": 8,
    "writer_examples": "",
    "limits": {
        "timezone": "America/New_York",
        "window_start": "09:00",
        "window_end": "11:00",
        "new_email_days": ["Tue", "Wed", "Thu"],
        "followup_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "ramp": [{"week": 1, "new_per_day": 2}],
        "max_total_per_day": 40,
        "min_gap_seconds": 180,
        "max_gap_seconds": 300,
        "bounce_pause_rate": 0.03,
        "bounce_min_sample": 10,
    },
    "sequence": {"followup_1_after_days": 4, "followup_2_after_days": 9,
                 "post_audit_followup_days": [3, 8], "later_recontact_days": 90},
    "targeting": {"website_check": ["Qualified"], "priority_order": ["A", "B", "C"],
                  "state_order": ["LA", "OH"], "require_mx_ok": True},
}

ENABLED = os.getenv("OUTREACH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    from . import db
    with db.connect() as con:
        raw = db.get_meta(con, "settings")
    return _merge(DEFAULTS, json.loads(raw) if raw else {})


def save(settings: dict):
    from . import db
    with db.connect() as con:
        db.set_meta(con, "settings", json.dumps(settings))


def is_configured(cfg=None) -> list[str]:
    """Return what's missing before sending can start (empty list = ready)."""
    cfg = cfg or load()
    missing = []
    if not cfg["sender"]["email"] or not cfg["sender"]["name"]:
        missing.append("sender settings (upload the settings file)")
    if not cfg["company"]["mailing_address"]:
        missing.append("mailing address")
    if not os.getenv("OUTREACH_MAIL_PASSWORD"):
        missing.append("OUTREACH_MAIL_PASSWORD variable in Railway")
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY variable in Railway")
    return missing


def mail_password():
    pw = os.environ.get("OUTREACH_MAIL_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError("OUTREACH_MAIL_PASSWORD is not set in Railway.")
    return pw


def require_anthropic_key():
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
