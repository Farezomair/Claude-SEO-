"""Robots doer — unblock AI crawlers in robots.txt.

`ai_crawler_blocked` means robots.txt Disallows an AI crawler (GPTBot, ClaudeBot,
PerplexityBot, …) from the whole site, so those pages can't be cited by AI search.
Appending an Allow can't clear it (the Disallow group is still there), so this
fetches the live robots.txt, drops the AI-blocking groups while keeping every other
rule, and installs the result as a full robots.txt override via the Bridge. It then
re-fetches and re-runs the auditor's own `_ai_blocked` check before closing the
finding. Reversible (clear the override). If a physical robots.txt file overrides
WordPress, the override won't take and the doer says so. Needs Bridge v8+.
"""
import re
import threading

import httpx

from .abilities import USER_AGENT
from .crawler import AI_CRAWLERS, _ai_blocked
from .database import SessionLocal
from .models import Finding, FixRecord, JobRun, RunLog, Site

_AI_LOWER = {b.lower() for b in AI_CRAWLERS}


def _base(conn: dict) -> str:
    u = conn["url"]
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _fetch_robots(site_url: str) -> str:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(site_url.rstrip("/") + "/robots.txt")
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _robots_post(conn: dict, payload: dict) -> bool:
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/robots", json=payload)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _strip_ai_blocks(robots_txt: str) -> tuple[str, list]:
    """Drop groups that Disallow an AI crawler from '/' (only when every agent in
    the group is an AI crawler, so we never touch a broad 'User-agent: *' rule).
    Returns (cleaned_robots, removed_bots)."""
    groups = re.split(r"\n\s*\n", robots_txt or "")
    kept, removed = [], set()
    for g in groups:
        agents = [m.strip().lower() for m in re.findall(r"(?im)^user-agent:\s*(.+)$", g)]
        disallows = [m.strip() for m in re.findall(r"(?im)^disallow:\s*(.*)$", g)]
        ai_here = [a for a in agents if a in _AI_LOWER]
        if ai_here and "/" in disallows and all(a in _AI_LOWER for a in agents):
            removed.update(ai_here)
            continue  # drop this AI-blocking group entirely
        kept.append(g)
    cleaned = "\n\n".join(g.strip() for g in kept if g.strip())
    return cleaned, sorted(removed)


def run_robots(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        findings = (db.query(Finding)
                    .filter(Finding.site_id == site_id, Finding.category == "ai_crawler_blocked",
                            Finding.status.in_(("open", "in-progress"))).all())
        if not findings:
            run.status = "completed"
            run.summary = "No AI-crawler blocks in robots.txt."
            db.commit()
            return

        robots = _fetch_robots(site.url)
        blocked = _ai_blocked(robots)

        def _close(remark):
            n = 0
            for f in findings:
                f.status = "closed"
                f.remark = remark
                n += 1
            return n

        if not blocked:
            n = _close("Resolved — robots.txt no longer blocks AI crawlers (verified live).")
            run.status = "completed"
            run.summary = f"robots.txt already allows AI crawlers — closed {n} finding(s)."
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        cleaned, removed = _strip_ai_blocks(robots)
        if not removed or cleaned == robots:
            run.status = "completed"
            run.summary = ("Couldn't isolate the AI-blocking rule to remove it safely — needs a manual "
                           "robots.txt edit.")
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        if not _robots_post(conn, {"full": cleaned}):
            run.status = "failed"
            run.summary = "Couldn't write robots.txt override — is SEO Agent Bridge (v8+) active?"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        still = _ai_blocked(_fetch_robots(site.url))
        if not still:
            n = _close(f"Auto-fixed: unblocked AI crawlers ({', '.join(removed)}) in robots.txt (verified live).")
            db.add(FixRecord(
                site_id=site_id, doer="Robots Agent", field="ai_crawler_blocked",
                action_taken=f"Removed robots.txt Disallow for {', '.join(removed)} (full override)",
                page_ref=site.url.rstrip("/") + "/robots.txt", before_value=", ".join(blocked),
                after_value="AI crawlers allowed", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
            run.status = "completed"
            run.summary = f"Unblocked AI crawlers ({', '.join(removed)}) in robots.txt — verified live, closed {n} finding(s)."
        else:
            run.status = "completed"
            run.summary = (f"Wrote the robots.txt override but it still blocks {', '.join(still)} — a physical "
                           "robots.txt file is likely overriding WordPress. Needs a manual edit.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Robots run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_robots_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_robots, args=(site_id, run_id, conn), daemon=True).start()
