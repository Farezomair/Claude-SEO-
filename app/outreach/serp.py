"""Live Google checks for prospects.

The checks are run by hand (Claude in Chrome, location set to the city, not logged in) and uploaded
on the Outreach page as a research file. No paid search API is used. This module stores and reads them.
"""
import json
from datetime import datetime, timedelta, timezone

from . import db

MAX_AGE_DAYS = 21


def add(prospect_id: int, checks: list[dict]):
    with db.connect() as con:
        for c in checks:
            con.execute("""INSERT INTO serp_checks(prospect_id,query,location,checked_at,maps_top,prospect_in_maps,
                           organic_top,prospect_organic,notes) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (prospect_id, c["query"], c["location"], c.get("checked_at") or db.now_iso(),
                         json.dumps(c.get("maps_top", [])), c.get("prospect_in_maps", ""),
                         json.dumps(c.get("organic_top", [])), c.get("prospect_organic", ""), c.get("notes", "")))
        db.log_event(con, prospect_id, "serp", f"{len(checks)} live checks added")


def recent(prospect_id: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM serp_checks WHERE prospect_id=? AND checked_at>=? ORDER BY id",
                           (prospect_id, cutoff)).fetchall()
    return [{"query": r["query"], "searched_from": r["location"], "date": (r["checked_at"] or "")[:10],
             "google_maps_top": json.loads(r["maps_top"] or "[]"), "prospect_in_maps": r["prospect_in_maps"],
             "page_one": json.loads(r["organic_top"] or "[]"), "prospect_on_page_one": r["prospect_organic"],
             "notes": r["notes"]} for r in rows]


def researched_ids() -> set:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    with db.connect() as con:
        rows = con.execute("SELECT DISTINCT prospect_id FROM serp_checks WHERE checked_at>=?", (cutoff,)).fetchall()
    return {r["prospect_id"] for r in rows}
