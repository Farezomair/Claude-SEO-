"""SEO front-end agent (Stage 4) — drafts blog content.

Content is riskier than a meta tag, so the agent never publishes directly. It
drafts a post with Claude and creates a pending Approval. The owner approves or
rejects it on the Approvals screen; only on approval is it sent to WordPress.
"""
import json
import threading

from .brain import generate_article
from .content_standard import strip_em_dashes
from .database import SessionLocal
from .models import Approval, Content, JobRun, RunLog, Site
from .rules import rules_for


def run_draft(site_id: int, run_id: int, topic: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        try:
            article = generate_article(site.name, site.url, topic, rules=rules_for("shared", "content"))
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Draft failed: {exc.__class__.__name__}: {exc}"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        if not article.get("title") or not article.get("body_html"):
            run.status = "failed"
            run.summary = "Draft failed: the model returned an empty article."
            db.commit()
            return

        content = Content(
            site_id=site_id,
            title=article["title"],
            body=strip_em_dashes(article["body_html"]),  # enforce the standard
            status="draft",
        )
        db.add(content)
        db.commit()
        db.refresh(content)

        db.add(Approval(
            site_id=site_id,
            kind="content",
            title=article["title"],
            summary=article.get("meta_description", "")[:500],
            payload=json.dumps({"content_id": content.id}),
            status="pending",
        ))
        run.status = "completed"
        run.summary = f"Drafted “{article['title']}” — waiting for your approval."
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:  # never let the thread die silently
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_draft_async(site_id: int, run_id: int, topic: str = "") -> None:
    threading.Thread(target=run_draft, args=(site_id, run_id, topic), daemon=True).start()
