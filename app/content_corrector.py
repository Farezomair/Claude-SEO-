"""Content Corrector (Phase D).

Cleans EXISTING blog content to the writing standard: strips em dashes and banned
AI vocabulary, removes filler, while preserving meaning. Meaning-preserving fixes
only. Operates on posts (classic/block content the WordPress content field
controls); it does not touch page-builder pages whose content lives elsewhere.

Drafts the cleaned version, holds it at the approval gate with the standard
asserted (zero banned terms, zero em dashes), and only updates the live post on
approval.
"""
import json
import threading

from .brain import correct_content
from .content_standard import scan, strip_em_dashes
from .database import SessionLocal
from .models import Approval, Content, JobRun, RunLog, Site
from .rules import rules_for
from .wordpress import WordPressClient, WordPressError

MAX_CORRECTIONS = 5


def run_correction(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])

        ok, code = wp.test()
        if not ok:
            run.status = "failed"
            run.summary = f"WordPress connection failed (HTTP {code})."
            db.commit()
            return

        rules = rules_for("shared", "content")
        posts = wp.list_content(kinds=("posts",), limit=40)
        drafted = 0
        for item in posts:
            if drafted >= MAX_CORRECTIONS:
                break
            issues = scan(item["content_text"])
            if not issues["banned"] and issues["em_dashes"] == 0:
                continue  # already clean

            try:
                result = correct_content(item["title"], item["content_html"], rules)
            except Exception as exc:
                db.add(RunLog(site_id=site_id, message=f"Correction failed for {item['link']}: {exc.__class__.__name__}"))
                db.commit()
                continue
            cleaned = strip_em_dashes(result.get("body_html") or "")
            if not cleaned:
                continue

            after = scan(cleaned)
            content = Content(site_id=site_id, title=item["title"], body=cleaned, status="draft")
            db.add(content)
            db.commit()
            db.refresh(content)

            removed = []
            if issues["banned"]:
                removed.append(f"{len(issues['banned'])} banned term(s)")
            if issues["em_dashes"]:
                removed.append(f"{issues['em_dashes']} em dash(es)")
            summary = ("Editorial cleanup (meaning preserved): removed " + ", ".join(removed)
                       + f". Result passes the standard: {'yes' if not after['banned'] and after['em_dashes'] == 0 else 'review'}.")
            db.add(Approval(
                site_id=site_id, kind="content_fix",
                title=f"Clean up: {item['title'] or item['link']}",
                summary=summary,
                payload=json.dumps({"content_id": content.id, "page_kind": item["kind"], "page_id": item["id"]}),
                status="pending",
            ))
            drafted += 1
            db.commit()

        run.status = "completed"
        run.summary = (f"Scanned {len(posts)} post(s), drafted {drafted} cleanup(s) — waiting for approval."
                       if drafted else f"Scanned {len(posts)} post(s); content already meets the standard.")
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


def start_correction_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_correction, args=(site_id, run_id, conn), daemon=True).start()
