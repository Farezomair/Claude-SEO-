"""Website agent (Stage 6) — makes reversible visual changes on demand.

The owner describes a change ("make the buttons orange and rounder"); the agent
reads the site's current Additional CSS, asks Claude for the complete new CSS,
and creates a pending Approval. Nothing touches the live site until approval —
and because the previous CSS is backed up, any applied change can be reverted in
one click. CSS-only by design: it cannot take the site down.
"""
import json
import threading

from .brain import generate_css
from .connections import get_connection
from .database import SessionLocal
from .models import Approval, JobRun, RunLog, Site, SiteChange
from .rules import rules_for
from .wordpress import WordPressClient, WordPressError


def run_change(site_id: int, run_id: int, request_text: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)

        conn = get_connection(site_id, site.url, site.name)
        if not conn:
            run.status = "failed"
            run.summary = "No WordPress connection — set it up in Settings first."
            db.commit()
            return

        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
        try:
            current_css = wp.get_custom_css()
        except WordPressError as exc:
            run.status = "failed"
            run.summary = str(exc)
            db.add(RunLog(site_id=site_id, message=f"Website change aborted: {exc}"))
            db.commit()
            return

        try:
            result = generate_css(site.name, site.url, request_text, current_css,
                                  rules_for("shared", "website"))
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Could not generate CSS: {exc.__class__.__name__}: {exc}"
            db.commit()
            return

        if not result.get("css"):
            run.status = "failed"
            run.summary = "The model returned no CSS."
            db.commit()
            return

        change = SiteChange(
            site_id=site_id, kind="website_css", request=request_text,
            css=result["css"], old_css=current_css, status="proposed",
        )
        db.add(change)
        db.commit()
        db.refresh(change)

        db.add(Approval(
            site_id=site_id, kind="website_css",
            title=f"Website change: {request_text[:80]}",
            summary=result.get("summary", ""),
            payload=json.dumps({"change_id": change.id}),
            status="pending",
        ))
        run.status = "completed"
        run.summary = f"Proposed a website change — waiting for your approval."
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_change_async(site_id: int, run_id: int, request_text: str) -> None:
    threading.Thread(target=run_change, args=(site_id, run_id, request_text), daemon=True).start()
