"""'Request amendment' worker — regenerate a pending proposal with the owner's
feedback, in the background, and update it in place. The approval stays pending so
the owner reviews the revised version before approving. Heavy generators (page
rewrites) run here off the request thread; `amend_note` on the Approval shows a
"reworking…" state until it finishes, then clears.
"""
import json
import threading

from bs4 import BeautifulSoup

from .brain import (correct_content, generate_article, generate_page,
                    generate_schema_jsonld, improve_meta, rewrite_page_html)
from .connections import get_connection
from .content_standard import strip_em_dashes
from .database import SessionLocal
from .models import Approval, Content, RunLog, Site, SiteChange
from .rules import rules_for
from .wordpress import WordPressClient

# Kinds the AI can revise. img_dims is purely mechanical (measured pixels), so it
# has nothing to "talk to the AI" about and is excluded in the UI.
AMENDABLE = {"content", "required_page", "content_fix", "meta_rewrite",
             "page_rewrite", "schema_inject"}


def _schema_script(jstr: str) -> str:
    return f'\n<script type="application/ld+json">\n{jstr}\n</script>\n'


def amend_proposal(approval_id: int, instructions: str) -> None:
    db = SessionLocal()
    try:
        appr = db.get(Approval, approval_id)
        if not appr or appr.status != "pending" or appr.kind not in AMENDABLE:
            if appr:
                appr.amend_note = ""
                db.commit()
            return
        site = db.get(Site, appr.site_id)
        payload = json.loads(appr.payload or "{}")
        kind = appr.kind
        try:
            if kind == "content":
                content = db.get(Content, payload.get("content_id"))
                if content:
                    res = generate_article(site.name, site.url, topic=content.title,
                                           rules=rules_for("shared", "content"), instructions=instructions)
                    if res.get("body_html"):
                        content.title = res.get("title") or content.title
                        content.body = strip_em_dashes(res["body_html"])
                        if res.get("meta_description"):
                            appr.summary = res["meta_description"]

            elif kind == "required_page":
                content = db.get(Content, payload.get("content_id"))
                page_type = payload.get("page_type", "")
                if content:
                    res = generate_page(site.name, site.url, page_type,
                                        rules=rules_for("shared", "website"), instructions=instructions)
                    if res.get("body_html"):
                        content.title = res.get("title") or content.title
                        content.body = strip_em_dashes(res["body_html"])
                        appr.title = f"Create {page_type} page: {content.title}"

            elif kind == "content_fix":
                content = db.get(Content, payload.get("content_id"))
                if content:
                    res = correct_content(content.title, content.body,
                                          rules=rules_for("shared", "content"), instructions=instructions)
                    if res.get("body_html"):
                        content.body = strip_em_dashes(res["body_html"])

            elif kind == "meta_rewrite":
                conn = get_connection(site.id, site.url, site.name)
                pk, pid = payload.get("page_kind", "posts"), payload.get("page_id")
                if conn and pid:
                    wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
                    item = next((it for it in wp.list_content(kinds=(pk,), limit=100) if it["id"] == pid), None)
                    if item:
                        res = improve_meta(item["title"], item["link"], item["content_text"],
                                           payload.get("new_title", ""), payload.get("new_desc", ""),
                                           site_name=site.name, rules=rules_for("shared", "seo_technical"),
                                           instructions=instructions)
                        if res.get("title"):
                            payload["new_title"] = res["title"]
                        if res.get("description"):
                            payload["new_desc"] = res["description"]
                        appr.summary = f"“{payload.get('new_title', '')}”."
                        appr.payload = json.dumps(payload)

            elif kind == "page_rewrite":
                change = db.get(SiteChange, payload.get("change_id"))
                if change:
                    res = rewrite_page_html(site.name, site.url, appr.title, change.old_css or "",
                                            rules=rules_for("shared", "website"), instructions=instructions)
                    if res.get("html"):
                        change.css = strip_em_dashes(res["html"])

            elif kind == "schema_inject":
                change = db.get(SiteChange, payload.get("change_id"))
                if change:
                    text = BeautifulSoup(change.old_css or "", "html.parser").get_text(" ", strip=True)
                    jl = generate_schema_jsonld(site.name, site.url, text, instructions=instructions)
                    jstr = json.dumps(jl, ensure_ascii=False, indent=2)
                    change.css = (change.old_css or "") + _schema_script(jstr)
                    payload["jsonld"] = jstr
                    appr.payload = json.dumps(payload)

            appr.amend_note = ""
            db.add(RunLog(site_id=site.id, message=f"Amended “{appr.title}” with your changes — ready for review."))
            db.commit()
        except Exception as exc:
            db.rollback()  # clear any failed transaction before re-reading
            appr = db.get(Approval, approval_id)
            if appr:
                appr.amend_note = (f"Amendment failed ({exc.__class__.__name__}). The previous "
                                   "version is unchanged — try again.")
                db.commit()
    finally:
        db.close()


def start_amend_async(approval_id: int, instructions: str) -> None:
    threading.Thread(target=amend_proposal, args=(approval_id, instructions), daemon=True).start()
