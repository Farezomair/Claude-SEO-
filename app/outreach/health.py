"""Health checks the Outreach page, the digest and the weekly research task use to spot anything that would stop email."""
import httpx

from . import config, db, mailer, sequence


def relay_ping():
    relay = mailer._relay()
    if not relay:
        return True, "direct SMTP (no relay configured)"
    url, user, pw = relay
    ping = url.rsplit("/", 1)[0] + "/ping"
    try:
        r = httpx.get(ping, auth=(user, pw), timeout=20)
    except Exception as e:
        return False, f"relay unreachable: {e}"
    if r.status_code == 200:
        return True, "relay OK"
    if r.status_code == 401:
        return False, ("relay login refused (401). WordPress Application Passwords may have been switched off "
                       "(Hostinger > Tools > Security, or Wordfence), or the app password was revoked.")
    return False, f"relay answered {r.status_code}: {r.text[:200]}"


def check() -> dict:
    ok, detail = relay_ping()
    with db.connect() as con:
        q = sequence.queue_status(con)
        paused = sequence.paused(con)
    missing = config.is_configured()
    healthy = ok and not paused and not missing and config.ENABLED and q["send_days_covered"] >= 3
    return {"healthy": healthy, "relay_ok": ok, "relay_detail": detail, "paused": paused, "missing": missing,
            "scheduler_enabled": config.ENABLED, **q}
