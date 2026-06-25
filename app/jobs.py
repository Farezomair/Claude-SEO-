"""Background audit runner.

A crawl can take many seconds, so it runs in a daemon thread rather than
blocking the web request. The thread opens its own database session (request
sessions can't cross thread boundaries) and writes the findings + a run-log
entry when it finishes.
"""
import threading

from .crawler import crawl_site
from .database import SessionLocal
from .models import Audit, AuditIssue, RunLog


def _run_audit(site_id: int, audit_id: int, start_url: str) -> None:
    db = SessionLocal()
    try:
        audit = db.get(Audit, audit_id)
        if audit is None:
            return
        try:
            result = crawl_site(start_url)
        except Exception as exc:  # never let the thread die silently
            audit.status = "failed"
            audit.summary = f"Audit failed: {exc.__class__.__name__}: {exc}"
            db.add(RunLog(site_id=site_id, message=f"Audit #{audit_id} failed: {exc}"))
            db.commit()
            return

        for iss in result["issues"]:
            db.add(AuditIssue(
                site_id=site_id, audit_id=audit_id,
                category=iss["category"], severity=iss["severity"],
                url=iss["url"], detail=iss["detail"],
            ))
        s = result["stats"]
        audit.status = "completed"
        audit.summary = (
            f"Crawled {s['pages_crawled']} pages, checked {s['links_checked']} links, "
            f"found {s['issues_found']} issue(s)."
        )
        db.add(RunLog(site_id=site_id, message=f"Audit #{audit_id} completed — {audit.summary}"))
        db.commit()
    finally:
        db.close()


def start_audit_async(site_id: int, audit_id: int, start_url: str) -> None:
    threading.Thread(
        target=_run_audit, args=(site_id, audit_id, start_url), daemon=True
    ).start()
