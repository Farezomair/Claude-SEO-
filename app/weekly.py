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

from .connections import get_connection
from .database import SessionLocal
from .jobs import _run_audit
from .models import Audit, Finding, FixRecord, JobRun, Report, RunLog, Site
from .seo_technical import run_metafix


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

        # Phase 1 — Audit (fresh source of truth -> routed Findings).
        audit = Audit(site_id=site_id, status="running", summary="Weekly audit…")
        db.add(audit)
        db.commit()
        db.refresh(audit)
        _run_audit(site_id, audit.id, site.url)  # synchronous
        issue_count = db.query(Finding).filter(Finding.audit_id == audit.id).count()
        steps.append(f"audited ({issue_count} finding(s))")

        # Phase 4 — Dispatch/execute (meta fixes, only if connected).
        conn = get_connection(site_id, site.url, site.name)
        fixes_applied = 0
        if conn:
            before = db.query(FixRecord).filter(FixRecord.site_id == site_id).count()
            mf = JobRun(site_id=site_id, kind="metafix", status="running", summary="Weekly meta fixes…")
            db.add(mf)
            db.commit()
            db.refresh(mf)
            run_metafix(site_id, mf.id, conn)  # synchronous (creates Findings + Fix records + verifies)
            fixes_applied = db.query(FixRecord).filter(FixRecord.site_id == site_id).count() - before
            steps.append(f"applied {fixes_applied} fix(es)")
        else:
            steps.append("skipped fixes (no connection)")

        # Phase 5 — Compile (report from Findings + Fix records).
        _build_report(db, site, audit, issue_count, fixes_applied)
        steps.append("wrote report")

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
