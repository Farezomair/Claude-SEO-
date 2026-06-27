"""Background audit runner.

A crawl can take many seconds, so it runs in a daemon thread rather than
blocking the web request. The thread opens its own database session (request
sessions can't cross thread boundaries) and writes the findings + a run-log
entry when it finishes.
"""
import json
import threading

from .crawler import crawl_site
from .database import SessionLocal
from .gsc import gsc_findings
from .models import Audit, Finding, RunLog
from .routing import classify
from .scoring import compute as compute_score


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

        # Crawl findings + Search Console findings (when connected) — both routed.
        all_issues = list(result["issues"])
        try:
            all_issues += gsc_findings(start_url)
        except Exception:
            pass  # GSC is best-effort; never let it break the audit

        for seq, iss in enumerate(all_issues, start=1):
            cls = classify(iss["category"])
            db.add(Finding(
                site_id=site_id, audit_id=audit_id,
                finding_key=f"WA-{site_id}-{audit_id}-{seq}",
                mode="audit", group=cls["group"], category=iss["category"],
                issue=iss["detail"], severity=iss["severity"],
                finding_type=iss.get("finding_type", "defect"),
                route=cls["route"], action_class=cls["action_class"],
                evidence_url=iss["url"], detection_source=iss.get("detection_source", "crawl"),
                status="open",
            ))
        # Score the audit (the rebuilt auditor: graded + prioritized, not a flat list).
        scored = compute_score(all_issues)
        audit.health_score = scored["overall"]
        audit.grade = scored["grade"]
        audit.category_scores = json.dumps(scored["categories"])
        audit.roadmap = json.dumps(scored["roadmap"])

        s = result["stats"]
        audit.status = "completed"
        audit.summary = (
            f"Health {scored['overall']}/100 ({scored['grade']}). "
            f"Crawled {s['pages_crawled']} pages, checked {s['links_checked']} links, "
            f"found {len(all_issues)} issue(s)."
        )
        db.add(RunLog(site_id=site_id, message=f"Audit #{audit_id} completed — {audit.summary}"))
        db.commit()
    finally:
        db.close()


def start_audit_async(site_id: int, audit_id: int, start_url: str) -> None:
    threading.Thread(
        target=_run_audit, args=(site_id, audit_id, start_url), daemon=True
    ).start()
