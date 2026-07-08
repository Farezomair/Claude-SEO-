"""The weekly loop — the Conductor (Phase A skeleton).

For one site, in phases:
1. Audit — re-audit, producing routed Findings (the source of truth).
2. Dispatch/execute — apply meta fixes (capped) if a WordPress connection exists,
   producing Fix records that verify themselves.
3. Compile — generate a progress report from Findings + Fix records.

Each step reuses the single-purpose agents. Runs synchronously in one background
thread; the scheduler (or a manual button) starts it. The full 7-phase work graph
and cross-week state land in Phase F.
"""
import threading
from collections import Counter

from .database import SessionLocal
from .dispatcher import dispatch_fixes
from .jobs import _run_audit
from .models import Audit, Finding, FixRecord, JobRun, Report, RunLog, Site


def _build_report(db, site, audit, issue_count: int, fixes_applied: int) -> None:
    findings = db.query(Finding).filter(Finding.audit_id == audit.id).all()
    by_route = Counter(f.route for f in findings)
    recent_fixes = (
        db.query(FixRecord)
        .filter(FixRecord.site_id == site.id, FixRecord.applied == True)  # noqa: E712
        .order_by(FixRecord.created_at.desc())
        .limit(max(fixes_applied, 5))
        .all()
    )
    verified = sum(1 for f in recent_fixes[:fixes_applied] if f.verification_verdict == "verified")

    parts = ["<h3>Audit</h3>"]
    if issue_count:
        parts.append("<p>Findings by owner:</p><ul>")
        for route, n in by_route.items():
            parts.append(f"<li>{n} routed to {route}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>No issues found — the site is clean. 🎉</p>")

    parts.append("<h3>Fixes applied this week</h3>")
    if fixes_applied:
        parts.append(f"<p>{verified} of {fixes_applied} verified.</p><ul>")
        for f in recent_fixes[:fixes_applied]:
            parts.append(f"<li>{f.field.replace('_', ' ')} — {f.after_value}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>No new fixes were needed this week.</p>")

    summary = f"{issue_count} finding(s), {fixes_applied} fix(es) applied ({verified} verified)."
    db.add(Report(site_id=site.id, summary=summary, body_html="\n".join(parts)))
    db.commit()


def run_weekly(site_id: int, weekly_run_id: int) -> None:
    db = SessionLocal()
    try:
        weekly = db.get(JobRun, weekly_run_id)
        site = db.get(Site, site_id)
        steps = []

        def _phase(name, findings=None, fixes=None):
            weekly.phase = name
            if findings is not None:
                weekly.findings_count = findings
            if fixes is not None:
                weekly.fixes_count = fixes
            db.commit()

        # Phase 0 — Strategy: make sure the keyword map exists (query-aware crew).
        _phase("audit")
        try:
            from .connections import get_connection
            from .keyword_brain import ensure_keyword_map
            conn0 = get_connection(site_id, site.url, site.name)
            if conn0:
                ensure_keyword_map(site_id, conn0)
        except Exception:
            pass

        # Phase 1 — Audit (fresh source of truth -> routed Findings).
        audit = Audit(site_id=site_id, status="running", summary="Weekly audit…")
        db.add(audit)
        db.commit()
        db.refresh(audit)
        _run_audit(site_id, audit.id, site.url)  # synchronous
        issue_count = db.query(Finding).filter(Finding.audit_id == audit.id).count()
        steps.append(f"audited ({issue_count} finding(s))")

        # Phase 2 — Route (findings are already classified/owned by the auditor).
        _phase("route", findings=issue_count)

        # Phase 4 — Dispatch: route open findings to their doers (safe fixes auto,
        # risky ones to Approvals).
        _phase("fix", findings=issue_count)
        disp = dispatch_fixes(site_id, progress_run_id=weekly_run_id)
        fixes_applied = disp["auto"]
        steps.append(disp["summary"])
        db.refresh(weekly)
        if weekly.status == "cancelled":
            weekly.summary = "Stopped by you."
            db.add(RunLog(site_id=site_id, message="Weekly run stopped by the owner."))
            db.commit()
            return

        # Phase 5 — Compile (report from Findings + Fix records).
        _phase("report", findings=issue_count, fixes=fixes_applied)
        _build_report(db, site, audit, issue_count, fixes_applied)
        steps.append("wrote report")

        # Phase 6 — Outcome loop: snapshot target-keyword positions (GSC).
        try:
            from .rank_tracker import take_snapshot
            n = take_snapshot(site_id, site.url)
            if n:
                steps.append(f"tracked {n} keyword position(s)")
        except Exception:
            pass

        _phase("done", findings=issue_count, fixes=fixes_applied)
        weekly.status = "completed"
        weekly.summary = "Weekly run: " + ", ".join(steps) + "."
        db.add(RunLog(site_id=site_id, message=weekly.summary))
        db.commit()
    except Exception as exc:  # never let the thread die silently
        weekly = db.get(JobRun, weekly_run_id)
        if weekly:
            weekly.status = "failed"
            weekly.summary = f"Weekly run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_weekly_async(site_id: int, weekly_run_id: int) -> None:
    threading.Thread(target=run_weekly, args=(site_id, weekly_run_id), daemon=True).start()
