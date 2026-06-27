"""SEO On-page agent (Phase C) — acts on Search Console ranking opportunities.

Takes the striking-distance and low-CTR Findings the SEO Auditor produced from
Search Console and rewrites each page's title + meta description to earn more
clicks. Gated: it drafts the new title/description and holds them at the approval
gate (these change what the page ranks for), then writes Yoast meta on approval.
"""
import json
import re
import threading
from urllib.parse import urlparse

from .brain import improve_meta
from .database import SessionLocal
from .models import Finding, JobRun, RunLog, Site
from .models import Approval
from .rules import rules_for
from .wordpress import YOAST_DESC_KEY, YOAST_TITLE_KEY, WordPressClient, WordPressError

MAX_REWRITES = 10


def _norm(url: str) -> str:
    return urlparse(url)._replace(fragment="", query="").geturl().rstrip("/")


def run_meta_rewrites(site_id: int, run_id: int, conn: dict) -> None:
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

        findings = (
            db.query(Finding)
            .filter(Finding.site_id == site_id, Finding.route == "SEO On-page",
                    Finding.category.in_(("striking_distance", "low_ctr")),
                    Finding.status == "open")
            .all()
        )
        if not findings:
            run.status = "completed"
            run.summary = "No ranking opportunities to act on."
            db.commit()
            return

        items = wp.list_content(limit=60)
        by_link = {_norm(it["link"]): it for it in items if it.get("link")}
        rules = rules_for("shared", "seo_technical")
        drafted = 0
        for f in findings:
            if drafted >= MAX_REWRITES:
                break
            item = by_link.get(_norm(f.evidence_url))
            if not item:
                continue  # can't match the GSC page to a WordPress page we can edit
            query_match = re.search(r'Query "(.*?)"', f.issue or "")
            query = query_match.group(1) if query_match else ""
            old_title = (item["meta"].get(YOAST_TITLE_KEY) or "").strip()
            old_desc = (item["meta"].get(YOAST_DESC_KEY) or "").strip()

            try:
                s = improve_meta(item["title"], item["link"], item["content_text"],
                                 old_title, old_desc, query, conn.get("site_name", ""), rules)
            except Exception as exc:
                db.add(RunLog(site_id=site_id, message=f"Meta rewrite failed for {item['link']}: {exc.__class__.__name__}"))
                db.commit()
                continue
            if not s.get("title") and not s.get("description"):
                continue

            summary = (f"Rewrite to improve clicks. "
                       f"Title: “{old_title or '(none)'}” → “{s['title']}”. "
                       f"Description: “{old_desc or '(none)'}” → “{s['description']}”.")
            db.add(Approval(
                site_id=site_id, kind="meta_rewrite",
                title=f"Improve ranking page: {item['title'] or item['link']}",
                summary=summary,
                payload=json.dumps({
                    "finding_id": f.id, "page_kind": item["kind"], "page_id": item["id"],
                    "new_title": s["title"], "new_desc": s["description"],
                    "old_title": old_title, "old_desc": old_desc,
                }),
                status="pending",
            ))
            f.status = "in-progress"
            drafted += 1
            db.commit()

        run.status = "completed"
        run.summary = (f"Drafted {drafted} ranking improvement(s) — waiting for approval."
                       if drafted else "No matching pages to improve (couldn't map them to editable pages).")
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


def start_meta_rewrites_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_meta_rewrites, args=(site_id, run_id, conn), daemon=True).start()
