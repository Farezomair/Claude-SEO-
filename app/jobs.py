"""Background audit runner.

A crawl can take many seconds, so it runs in a daemon thread rather than
blocking the web request. The thread opens its own database session (request
sessions can't cross thread boundaries) and writes the findings + a run-log
entry when it finishes.
"""
import json
import threading

from .content_analyzer import analyze_site_content
from .crawler import crawl_site
from .database import SessionLocal
from .gsc import gsc_findings
from .models import Audit, Finding, RunLog, Site
from .perf import analyze_performance
from .routing import classify
from .scoring import MEASURED, compute as compute_score


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

        # Phase 2 specialists (best-effort; never break the audit).
        measured = set(MEASURED)
        site = db.get(Site, site_id)
        try:
            all_issues += analyze_site_content(start_url, site.name if site else "")
        except Exception:
            pass
        try:
            perf_issues, perf_ok = analyze_performance(start_url)
            all_issues += perf_issues
            if perf_ok:
                measured.add("performance")
        except Exception:
            pass

        # The fresh audit is the source of truth: retire prior live findings so
        # they can't get stuck (in-progress with a lost approval) or accumulate
        # across runs. Anything still wrong is re-detected below as a new finding.
        (db.query(Finding)
         .filter(Finding.site_id == site_id,
                 Finding.status.in_(("open", "in-progress", "reopened", "escalated",
                                     "needs-human", "snoozed", "no-capability")))
         .update({Finding.status: "superseded"}, synchronize_session=False))
        db.commit()

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
        scored = compute_score(all_issues, measured)
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
