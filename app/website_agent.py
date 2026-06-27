"""Website agent (Stage 6) — makes reversible visual changes on demand.

The owner describes a change ("make the buttons orange and rounder"); the agent
reads the site's current Additional CSS, asks Claude for the complete new CSS,
and creates a pending Approval. Nothing touches the live site until approval —
and because the previous CSS is backed up, any applied change can be reverted in
one click. CSS-only by design: it cannot take the site down.
"""
import json
import re
import threading

from .brain import generate_css, generate_page
from .connections import get_connection
from .content_standard import strip_em_dashes
from .database import SessionLocal
from .models import Approval, Content, Finding, JobRun, RunLog, Site, SiteChange
from .rules import rules_for
from .wordpress import WordPressClient, WordPressError


def _page_type(issue: str) -> str:
    m = re.search(r"No (\w+) page", issue or "")
    return m.group(1).lower() if m else "page"


def run_page_drafts(site_id: int, run_id: int) -> None:
    """Draft the missing required pages the auditor flagged, route to approval."""
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        findings = (
            db.query(Finding)
            .filter(Finding.site_id == site_id,
                    Finding.category == "required_page_missing",
                    Finding.status == "open")
            .all()
        )
        if not findings:
            run.status = "completed"
            run.summary = "No missing required pages to draft."
            db.commit()
            return

        drafted = 0
        for f in findings:
            page_type = _page_type(f.issue)
            try:
                page = generate_page(site.name, site.url, page_type, rules_for("shared", "content"))
            except Exception as exc:
                db.add(RunLog(site_id=site_id, message=f"Page draft failed for {page_type}: {exc.__class__.__name__}"))
                db.commit()
                continue
            if not page.get("title") or not page.get("body_html"):
                continue

            content = Content(site_id=site_id, title=page["title"],
                              body=strip_em_dashes(page["body_html"]), status="draft")
            db.add(content)
            db.commit()
            db.refresh(content)

            summary = "New page draft — review before publishing."
            if page.get("legal"):
                summary += " This is a legal page template — review with a qualified professional and fill in the [bracketed] placeholders before publishing."
            db.add(Approval(
                site_id=site_id, kind="required_page",
                title=f"Create {page_type} page: {page['title']}",
                summary=summary,
                payload=json.dumps({"content_id": content.id, "finding_id": f.id, "page_type": page_type}),
                status="pending",
            ))
            f.status = "in-progress"
            drafted += 1
            db.commit()

        run.status = "completed"
        run.summary = f"Drafted {drafted} missing page(s) — waiting for your approval."
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


def start_page_drafts_async(site_id: int, run_id: int) -> None:
    threading.Thread(target=run_page_drafts, args=(site_id, run_id), daemon=True).start()


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
