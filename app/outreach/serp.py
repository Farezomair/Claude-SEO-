"""Live Google checks through serper.dev: who shows up in the Maps pack and on page one."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from . import db, llm

MAX_AGE_DAYS = 21
STATE_NAMES = {"LA": "Louisiana", "OH": "Ohio", "TX": "Texas", "MS": "Mississippi", "AL": "Alabama", "FL": "Florida"}


class SearchPlan(BaseModel):
    searches: list["PlannedSearch"] = Field(description="2 or 3 searches, most important first")


class PlannedSearch(BaseModel):
    query: str = Field(description="exactly what a family member would type, lowercase, e.g. 'drug rehab monroe la', 'detox lafayette la', 'iop baton rouge'")
    location: str = Field(description="the city they search from: the center's city, or the nearest bigger city families come from, as 'City, State, United States'")


SearchPlan.model_rebuild()

PLAN_SYSTEM = """You pick the Google searches a worried family would run to find a treatment center like this one, so we can check who shows up. Only money searches: the kind that lead to a call (drug rehab, detox, IOP, residential, men's or women's rehab, plus the city). Only services the center offers. Never informational searches. Prefer the city where most nearby families live: if the center sits in a small town, use the nearest bigger city it serves."""


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def _same_business(title: str, brand: str, domain: str, website: str = "") -> bool:
    if domain and website and domain in website.lower():
        return True
    stop = {"the", "of", "and", "center", "centers", "recovery", "treatment", "rehab", "addiction", "llc", "inc", "behavioral", "health"}
    a = {w for w in _norm(title).split() if w not in stop}
    b = {w for w in _norm(brand).split() if w not in stop}
    return bool(b) and b <= a


def _serper(query: str, location: str) -> dict:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("SERPER_API_KEY is not set in Railway.")
    r = httpx.post("https://google.serper.dev/search", timeout=30,
                   headers={"X-API-KEY": key, "Content-Type": "application/json"},
                   json={"q": query, "location": location, "gl": "us", "hl": "en", "num": 20})
    r.raise_for_status()
    return r.json()


def check_prospect(prospect: dict, site_summary: str, brand: str) -> list[dict]:
    """Plan 2-3 searches, run them, store and return the checks."""
    listing = {k: prospect.get(k) for k in ("center", "state", "cities", "levels_of_care", "website")}
    plan = llm.structured(PLAN_SYSTEM, "Center:\n" + llm.as_json(listing) + "\n\nWhat their site says (short):\n" + site_summary[:3000],
                          SearchPlan, effort="low", max_tokens=4000)
    domain = (prospect.get("domain") or urlparse(prospect.get("website") or "").netloc).lower().removeprefix("www.")
    checks = []
    for s in plan.searches[:3]:
        try:
            data = _serper(s.query, s.location)
        except Exception as e:
            checks.append({"query": s.query, "location": s.location, "error": str(e)})
            continue
        places = data.get("places") or []
        maps_top = [p.get("title", "") for p in places[:3]]
        in_maps = "no"
        for i, p in enumerate(places[:10], start=1):
            if _same_business(p.get("title", ""), brand, domain, p.get("website", "")):
                in_maps = f"#{i}"
                break
        if not places:
            in_maps = "no map pack shown"
        organic = data.get("organic") or []
        top = []
        prospect_pos = "not in the top 20"
        for o in organic[:20]:
            host = urlparse(o.get("link", "")).netloc.lower().removeprefix("www.")
            pos = o.get("position") or 0
            if pos <= 10:
                top.append(f"#{pos} {o.get('title', '')[:70]} ({host})")
            if domain and host.endswith(domain) and prospect_pos == "not in the top 20":
                where = "page one" if pos <= 10 else "page two"
                prospect_pos = f"#{pos} on {where} ({o.get('title', '')[:60]})"
        checks.append({"query": s.query, "location": s.location.split(",")[0] + ", " + (prospect.get("state") or "")[:2],
                       "maps_top": maps_top, "prospect_in_maps": in_maps, "organic_top": top,
                       "prospect_organic": prospect_pos, "notes": "serper.dev, location set, not logged in"})
    add(prospect["id"], [c for c in checks if "error" not in c])
    return checks


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
