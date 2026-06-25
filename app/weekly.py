"""The weekly loop — the Master Master Agent conductor (Stage 5).

For one site, in order:
1. Re-audit the site (the source of truth for what to work on).
2. Apply meta fixes (capped) if a WordPress connection exists.
3. Generate a progress report.

Each step reuses the existing single-purpose agents. Runs synchronously inside
one background thread; the scheduler (or a manual button) starts it.
"""
import threading
from collections import Counter

from .connections import get_connection
from .database import SessionLocal
from .jobs import _run_audit
from .models import Audit, AuditIssue, Fix, JobRun, Report, RunLog, Site
from .seo_technical import run_metafix


def _build_report(db, site, audit, issue_count: int, fixes_applied: int) -> None:
    issues = db.query(AuditIssue).filter(AuditIssue.audit_id == audit.id).all()
    by_cat = Counter(i.category.replace("_", " ") for i in issues)
    recent_fixes = (
        db.query(Fix)
        .filter(Fix.site_id == site.id, Fix.status.in_(["verified", "applied"]))
        .order_by(Fix.created_at.desc())
        .limit(max(fixes_applied, 5))
        .all()
    )

    parts = ["<h3>Audit</h3>"]
    if issue_count:
        parts.append("<ul>")
        for cat, n in by_cat.items():
            parts.append(f"<li>{n} × {cat}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>No issues found — the site is clean. 🎉</p>")

    parts.append("<h3>Fixes applied this week</h3>")
    if fixes_applied:
        parts.append("<ul>")
        for f in recent_fixes[:fixes_applied]:
            parts.append(f"<li>{f.field.replace('_', ' ')} — {f.new_value}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>No new fixes were needed this week.</p>")

    summary = f"{issue_count} issue(s) found, {fixes_applied} fix(es) applied."
    db.add(Report(site_id=site.id, summary=summary, body_html="\n".join(parts)))
    db.commit()


def run_weekly(site_id: int, weekly_run_id: int) -> None:
    db = SessionLocal()
    try:
        weekly = db.get(JobRun, weekly_run_id)
        site = db.get(Site, site_id)
        steps = []

        # 1. Re-audit (fresh source of truth).
        audit = Audit(site_id=site_id, status="running", summary="Weekly audit…")
        db.add(audit)
        db.commit()
        db.refresh(audit)
        _run_audit(site_id, audit.id, site.url)  # synchronous
        issue_count = db.query(AuditIssue).filter(AuditIssue.audit_id == audit.id).count()
        steps.append(f"audited ({issue_count} issue(s))")

        # 2. Meta fixes (only if connected).
        conn = get_connection(site_id, site.url, site.name)
        fixes_applied = 0
        if conn:
            before = db.query(Fix).filter(Fix.site_id == site_id).count()
            mf = JobRun(site_id=site_id, kind="metafix", status="running", summary="Weekly meta fixes…")
            db.add(mf)
            db.commit()
            db.refresh(mf)
            run_metafix(site_id, mf.id, conn)  # synchronous
            fixes_applied = db.query(Fix).filter(Fix.site_id == site_id).count() - before
            steps.append(f"applied {fixes_applied} fix(es)")
        else:
            steps.append("skipped fixes (no connection)")

        # 3. Report.
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
