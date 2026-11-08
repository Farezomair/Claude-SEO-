"""Load prospects (Excel upload) and an exported state file into the outreach tables."""
import io
import json
from urllib.parse import urlparse

import openpyxl
from sqlalchemy import text

from ..database import engine
from . import config, db

JUNK_EMAIL_PARTS = ("example@", "mysite.com", "domain.com", "sentry", "wixpress", "@email.com")


def _clean(v):
    return str(v).strip() if v not in (None, "") else None


def import_prospects_xlsx(data: bytes) -> tuple[int, int]:
    """Add new prospects from the workbook's 'Prospects' sheet. Existing emails are left untouched."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["Prospects"] if "Prospects" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h).strip() if h else "" for h in rows[0]]
    col = {h: i for i, h in enumerate(head)}
    added = skipped = 0
    with db.connect() as con:
        for n, r in enumerate(rows[1:], start=2):
            def g(k):
                return _clean(r[col[k]]) if k in col and col[k] < len(r) else None
            email = (g("Best email") or "").lower() or None
            if not email or any(j in email for j in JUNK_EMAIL_PARTS):
                skipped += 1
                continue
            if con.execute("SELECT 1 AS x FROM prospects WHERE email=?", (email,)).fetchone():
                continue
            website = g("Website")
            domain = urlparse(website).netloc.lower().removeprefix("www.") if website else None
            others = [e.strip().lower() for e in (g("All emails found") or "").split(",")
                      if e.strip() and e.strip().lower() != email and not any(j in e.lower() for j in JUNK_EMAIL_PARTS)]
            sheet_status = (g("Status") or "Not contacted").lower()
            status = "new" if sheet_status == "not contacted" else "imported_" + sheet_status.replace(" ", "_")
            con.execute(
                """INSERT INTO prospects(center,website,domain,state,cities,levels_of_care,priority,website_check,
                   email,mx_ok,other_emails,phone,status,notes,sheet_row,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (g("Center"), website, domain, g("State"), g("Cities"), g("Levels of care"), g("Priority"),
                 g("Website check"), email, g("Best email MX ok"), ", ".join(others), g("Phone"), status,
                 g("Notes"), n, db.now_iso(), db.now_iso()))
            added += 1
    return added, skipped


STATE_TABLES = ["prospects", "research", "messages", "serp_checks", "events"]


def import_state(data: bytes) -> dict:
    """Replace all outreach data with an exported state file (settings, tables, meta)."""
    state = json.loads(data.decode("utf8"))
    counts = {}
    with db.connect() as con:
        for t in reversed(STATE_TABLES):
            con.execute(f"DELETE FROM {t}")
        for t in STATE_TABLES:
            rows = state.get(t) or []
            for row in rows:
                cols = list(row.keys())
                con.execute(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                            [row[c] for c in cols])
            counts[t] = len(rows)
        for k, v in (state.get("meta") or {}).items():
            if k != "settings":
                db.set_meta(con, k, v)
    if state.get("settings"):
        config.save(state["settings"])
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for t in ("prospects", "messages", "serp_checks", "events"):
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('outreach_{t}','id'), "
                                  f"COALESCE((SELECT MAX(id) FROM outreach_{t}), 1))"))
    return counts
