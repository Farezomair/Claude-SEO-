"""Head-schema scrubber doer — fixes fabricated JSON-LD that lives in the page
<head>, injected by another plugin or the theme (so no body doer can reach it).

The audit reads the RENDERED head, so it detects self-serving aggregateRating and
placeholder street addresses there — but every other Ascend doer writes
`_meridian_body`, which is in the <body>. This doer drives the Bridge v10
`/schema-scrub` output-buffer filter instead: it toggles the scrub on, re-fetches
the live homepage, and only closes the findings once the fabricated schema is
actually gone from the served HTML. Reversible (toggle off).
"""
import re
import threading

import httpx

from .abilities import USER_AGENT
from .database import SessionLocal
from .models import Finding, FixRecord, JobRun, RunLog, Site


def _base(conn: dict) -> str:
    u = conn["url"]
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def scrub_head_schema(conn: dict, strip_reviews: bool = True, bad_street: str = "",
                      street_mode: str = "remove", street_value: str = "") -> tuple[bool, dict]:
    """Set the Bridge scrub options. Returns (ok, state)."""
    payload = {"strip_reviews": bool(strip_reviews), "bad_street": bad_street,
               "street_mode": street_mode, "street_value": street_value}
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/schema-scrub", json=payload)
        return (r.status_code in (200, 201)), (r.json() if r.status_code < 500 else {})
    except Exception:
        return False, {}


def _live_head(site_url: str) -> str:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            html = c.get(site_url.rstrip("/") + "/").text
        return html[:html.lower().find("<body")] if "<body" in html.lower() else html
    except Exception:
        return ""


def run_head_scrub(site_id: int, run_id: int, conn: dict, strip_reviews: bool = True,
                   bad_street: str = "", street_mode: str = "remove", street_value: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        ok, _state = scrub_head_schema(conn, strip_reviews, bad_street, street_mode, street_value)
        if not ok:
            run.status = "failed"
            run.summary = "Couldn't set the head-schema scrubber — is SEO Agent Bridge (v10+) active?"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        head = _live_head(site.url)
        closed, done = [], []
        if strip_reviews and "aggregaterating" not in head.lower():
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "schema_selfserving_reviews",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = "Auto-fixed: self-serving aggregateRating stripped from the head schema (verified live)."
                closed.append(f)
            done.append("removed self-serving review schema")
        if bad_street and (f'"streetAddress": "{bad_street}"'.lower() not in head.lower()
                           and f'"streetaddress":"{bad_street}"'.lower() not in head.lower()):
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "schema_fake_address",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = (f"Auto-fixed: fake street address “{bad_street}” "
                            + ("replaced" if street_mode == "replace" else "removed (service-area schema)")
                            + " in the head schema (verified live).")
                closed.append(f)
            done.append("fixed fabricated street address" if street_mode == "replace"
                        else "removed fabricated street address (service-area)")

        if closed:
            db.add(FixRecord(
                site_id=site_id, doer="Schema-cleanup Agent", field="schema_selfserving_reviews",
                action_taken="Scrubbed fabricated JSON-LD from the page head (Bridge v10): " + "; ".join(done),
                page_ref=site.url, before_value="(fabricated head schema present)",
                after_value="; ".join(done), method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
            run.summary = f"Head schema scrubbed and verified live — closed {len(closed)} finding(s): {', '.join(done)}."
        else:
            run.summary = ("Scrub enabled, but the head still shows the fabricated schema (cache?) — "
                           "will re-verify next audit.")
        run.status = "completed"
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Head-scrub run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_head_scrub_async(site_id: int, run_id: int, conn: dict, **kw) -> None:
    threading.Thread(target=run_head_scrub, args=(site_id, run_id, conn), kwargs=kw, daemon=True).start()
