"""The Dispatcher — routes open audit findings to the doer that owns each one.

This is what makes the Command Center "Fix" stage actually work through the audit
instead of running a single fixer. Policy (owner's choice):
- SAFE fixes (meta titles/descriptions) are applied automatically, no approval.
- RISKY fixes (unique titles, missing pages) are proposed to the Approvals gate.
- Findings with no doer yet (or needs-human) are left open and surfaced in the
  audit's prioritized plan.

Doers are reused as-is; each opens its own DB session and records its own JobRun +
FixRecords/Approvals, so the dispatcher just decides what to run based on which
finding categories are currently open.
"""
import threading

from .connections import get_connection
from .database import SessionLocal
from .models import Approval, Finding, FixRecord, JobRun, RunLog, Site
from .schema_agent import run_schema_inject
from .seo_technical import run_dedupe_titles, run_metafix
from .website_agent import run_page_drafts

# Auto-safe lane: SEO Technical writes these via Yoast with verify-after-write.
META_CATS = {"meta_title", "meta_description", "missing_title", "title_length",
             "meta_description_missing"}


def _open_count(db, site_id: int, cats) -> int:
    return (
        db.query(Finding)
        .filter(Finding.site_id == site_id, Finding.status == "open",
                Finding.category.in_(cats))
        .count()
    )


def dispatch_fixes(site_id: int) -> dict:
    """Run every applicable doer over the site's open findings. Returns counts."""
    db = SessionLocal()
    auto = 0
    steps: list[str] = []
    try:
        site = db.get(Site, site_id)
        conn = get_connection(site_id, site.url, site.name)
        if not conn:
            return {"auto": 0, "proposed": 0,
                    "summary": "No WordPress connection — connect one in Settings to apply fixes."}

        pending_before = (
            db.query(Approval).filter(Approval.site_id == site_id,
                                      Approval.status == "pending").count()
        )

        # --- AUTO-SAFE lane: meta titles + descriptions (applied directly) ---
        if _open_count(db, site_id, META_CATS):
            before = db.query(FixRecord).filter(FixRecord.site_id == site_id).count()
            mf = JobRun(site_id=site_id, kind="metafix", status="running",
                        summary="Auto-fixing meta titles & descriptions…")
            db.add(mf)
            db.commit()
            db.refresh(mf)
            run_metafix(site_id, mf.id, conn)  # synchronous; applies + verifies
            auto = db.query(FixRecord).filter(FixRecord.site_id == site_id).count() - before
            if auto:
                steps.append(f"auto-applied {auto} meta fix(es)")

        # --- GATED lane: duplicate titles -> Approvals ---
        if _open_count(db, site_id, {"duplicate_title"}):
            dr = JobRun(site_id=site_id, kind="dedupe", status="running",
                        summary="Proposing unique titles…")
            db.add(dr)
            db.commit()
            db.refresh(dr)
            run_dedupe_titles(site_id, dr.id, conn)

        # --- GATED lane: missing required pages -> Approvals ---
        if _open_count(db, site_id, {"required_page_missing"}):
            pr = JobRun(site_id=site_id, kind="pagedraft", status="running",
                        summary="Drafting missing pages…")
            db.add(pr)
            db.commit()
            db.refresh(pr)
            run_page_drafts(site_id, pr.id)

        # --- GATED lane: homepage entity/LocalBusiness schema -> Approvals ---
        schema_pending = (
            db.query(Approval).filter(Approval.site_id == site_id,
                                      Approval.kind == "schema_inject",
                                      Approval.status == "pending").count()
        )
        if not schema_pending and _open_count(db, site_id, {"no_entity_schema", "no_localbusiness_schema"}):
            sj = JobRun(site_id=site_id, kind="schema", status="running",
                        summary="Generating homepage schema…")
            db.add(sj)
            db.commit()
            db.refresh(sj)
            run_schema_inject(site_id, sj.id, conn)

        pending_after = (
            db.query(Approval).filter(Approval.site_id == site_id,
                                      Approval.status == "pending").count()
        )
        proposed = max(0, pending_after - pending_before)
        if proposed:
            steps.append(f"sent {proposed} change(s) to Approvals")

        summary = "; ".join(steps) if steps else "No auto-fixable findings right now."
        db.add(RunLog(site_id=site_id, message=f"Dispatcher: {summary}"))
        db.commit()
        return {"auto": auto, "proposed": proposed, "summary": summary}
    except Exception as exc:
        return {"auto": auto, "proposed": 0,
                "summary": f"Dispatch failed: {exc.__class__.__name__}: {exc}"}
    finally:
        db.close()


def start_dispatch_async(site_id: int, run_id: int) -> None:
    def _run():
        result = dispatch_fixes(site_id)
        db = SessionLocal()
        try:
            run = db.get(JobRun, run_id)
            if run:
                run.status = "completed"
                run.summary = result["summary"]
                run.fixes_count = result["auto"]
                db.commit()
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True).start()
