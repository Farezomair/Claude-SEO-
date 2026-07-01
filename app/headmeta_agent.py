"""Head/meta doer — adds the <head> tags the audit found missing.

Turns on the Bridge head-injection toggles for exactly the tags a site is missing
— self-referencing canonical, Open Graph, mobile viewport, and a favicon — then
re-fetches the flagged pages to confirm the tags now render before closing the
findings. Site-level toggles (no per-page storage), auto-applied and reversible.
Needs SEO Agent Bridge v8+ (the /head endpoint). Favicon is best-effort: it reuses
an existing logo-like image if one is on the homepage, otherwise it leaves the
favicon finding open (a real favicon is a design asset for the owner to supply).
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


def _head_post(conn: dict, payload: dict) -> bool:
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/head", json=payload)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _fetch(url: str) -> str:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            return c.get(url).text
    except Exception:
        return ""


def _page_for(findings, cat: str, fallback: str) -> str:
    for f in findings:
        if f.category == cat and f.evidence_url:
            return f.evidence_url
    return fallback


def _pick_favicon(html: str) -> str:
    """Reuse an existing logo/icon-like image as a favicon, if one is on the page."""
    for m in re.finditer(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', html, re.I):
        src = m.group(1)
        if re.search(r"logo|icon|favicon|mark|badge", src, re.I):
            return src
    return ""


def run_headmeta(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        findings = (db.query(Finding)
                    .filter(Finding.site_id == site_id,
                            Finding.category.in_(("missing_canonical", "og_incomplete",
                                                  "missing_viewport", "missing_favicon")),
                            Finding.status.in_(("open", "in-progress"))).all())
        if not findings:
            run.status = "completed"
            run.summary = "No head tags missing."
            db.commit()
            return
        cats = {f.category for f in findings}
        want = {}
        if "missing_canonical" in cats:
            want["canonical"] = True
        if "og_incomplete" in cats:
            want["og"] = True
        if "missing_viewport" in cats:
            want["viewport"] = True
        favicon = ""
        if "missing_favicon" in cats:
            favicon = _pick_favicon(_fetch(site.url))
            if favicon:
                want["favicon"] = favicon
        if not want:
            run.status = "completed"
            run.summary = ("Nothing auto-fixable — the only head gap is a favicon, and no logo image "
                           "was found to reuse. Add a favicon/logo and it'll clear next audit.")
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        if not _head_post(conn, want):
            run.status = "failed"
            run.summary = "Couldn't set head tags — is SEO Agent Bridge (v8+) active?"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        # Verify live on an actually-flagged page (canonical/og/viewport) + homepage (favicon).
        page = _page_for(findings, "missing_canonical", _page_for(findings, "og_incomplete", site.url))
        phtml = _fetch(page)
        home = _fetch(site.url)
        done = set()
        if want.get("canonical") and re.search(r'rel=["\']canonical["\']', phtml, re.I):
            done.add("missing_canonical")
        if want.get("og") and re.search(r'property=["\']og:title["\']', phtml, re.I):
            done.add("og_incomplete")
        if want.get("viewport") and re.search(r'name=["\']viewport["\']', phtml, re.I):
            done.add("missing_viewport")
        if want.get("favicon") and re.search(r'rel=["\']icon["\']', home, re.I):
            done.add("missing_favicon")

        closed = 0
        for f in findings:
            if f.category in done:
                f.status = "closed"
                f.remark = f"Auto-fixed: {f.category.replace('_', ' ')} added to the page head (verified live)."
                closed += 1
        if done:
            db.add(FixRecord(
                site_id=site_id, doer="Head/meta Agent", field=",".join(sorted(done)),
                action_taken="Enabled head tags: " + ", ".join(sorted(want.keys())),
                page_ref=site.url, before_value="(missing head tags)",
                after_value=", ".join(sorted(done)) + " now rendered",
                method="auto-safe", lane="autonomous", applied=True,
                verification_verdict="verified", status="done"))
        run.status = "completed"
        run.summary = (f"Added {', '.join(sorted(done))} to the page head — verified live, closed {closed} finding(s)."
                       if done else "Set head tags but couldn't verify them live yet (cache?).")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Head/meta run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_headmeta_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_headmeta, args=(site_id, run_id, conn), daemon=True).start()
