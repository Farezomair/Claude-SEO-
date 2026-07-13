"""Approval actions — the ONE implementation of approve/reject.

Moved verbatim from the /approvals routes so every caller (the approval buttons
AND the Fix Chat) runs exactly the same apply logic: same WordPress writes, same
verification, same FixRecords. Returns (ok, notice_key) instead of redirects.
"""
import json
import os

from .abilities import AbilitiesClient
from .connections import get_connection
from .content_standard import scan
from .elementor_agent import read_body, write_body
from .models import Approval, Content, Finding, FixRecord, RunLog, Site, SiteChange, utcnow
from .wordpress import WordPressClient, WordPressError, YOAST_DESC_KEY, YOAST_TITLE_KEY

CONTENT_PUBLISH_STATUS = os.getenv("CONTENT_PUBLISH_STATUS", "draft")


def _flush_cache(conn) -> None:
    """Best-effort LiteSpeed cache flush so a gated live write shows immediately."""
    try:
        AbilitiesClient(conn["url"], conn["username"], conn["app_password"]).run(
            "hostinger-ai-assistant/litespeed-cache-flush", {})
    except Exception:
        pass


def apply_approval(db, appr: Approval, publish: bool) -> tuple[bool, str]:
    """Apply one pending approval. Returns (ok, notice): notice is 'approved' on
    success, else 'no_connection' / 'publish_fail' (matching the UI notices)."""
    site = db.get(Site, appr.site_id)
    if appr.kind == "content":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return False, "no_connection"
        status = "publish" if publish else CONTENT_PUBLISH_STATUS
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_post(content.title, content.body, status=status, excerpt=appr.summary)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Publish failed for “{appr.title}”: {exc}"))
            db.commit()
            return False, "publish_fail"
        content.status = "published" if status == "publish" else "in_wordpress_draft"
        db.add(RunLog(
            site_id=site.id,
            message=f"Approved & sent to WordPress ({result.get('status')}): {content.title} {result.get('link', '')}",
        ))

    elif appr.kind == "meta_rewrite":
        payload = json.loads(appr.payload or "{}")
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return False, "no_connection"
        kind, page_id = payload.get("page_kind", "posts"), payload.get("page_id")
        new_title, new_desc = payload.get("new_title", ""), payload.get("new_desc", "")
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            meta = {}
            if new_title:
                meta[YOAST_TITLE_KEY] = new_title
            if new_desc:
                meta[YOAST_DESC_KEY] = new_desc
            wp.update_meta(kind, page_id, meta)
            live = wp.get_meta(kind, page_id)
            verified = (
                (not new_title or (live.get(YOAST_TITLE_KEY) or "").strip() == new_title.strip())
                and (not new_desc or (live.get(YOAST_DESC_KEY) or "").strip() == new_desc.strip())
            )
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Meta rewrite failed for “{appr.title}”: {exc}"))
            db.commit()
            return False, "publish_fail"
        _flush_cache(conn)
        finding = db.get(Finding, payload.get("finding_id"))
        if finding:
            finding.status = "closed"
        db.add(FixRecord(
            site_id=site.id, finding_id=payload.get("finding_id"), doer="SEO On-page",
            action_taken=f"Rewrote title/description: {new_title}", page_ref=str(page_id),
            field="title+description", before_value=f"{payload.get('old_title', '')} | {payload.get('old_desc', '')}",
            after_value=f"{new_title} | {new_desc}", method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
            outcome_pending=True,  # ranking result lags
        ))
        db.add(RunLog(site_id=site.id, message=f"Applied ranking rewrite: {new_title}"))

    elif appr.kind == "content_fix":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not content:
            return False, "no_connection"
        kind, page_id = payload.get("page_kind", "posts"), payload.get("page_id")
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            # Snapshot current content for rollback, then update.
            before = ""
            for it in wp.list_content(kinds=(kind,), limit=60):
                if it["id"] == page_id:
                    before = it["content_html"]
                    break
            wp.update_content(kind, page_id, content.body)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Content cleanup failed for “{appr.title}”: {exc}"))
            db.commit()
            return False, "publish_fail"
        _flush_cache(conn)
        after_clean = scan(content.body)
        db.add(FixRecord(
            site_id=site.id, doer="Content Corrector",
            action_taken=f"Editorial cleanup of {content.title}",
            page_ref=str(page_id), before_value=before[:5000], after_value=content.body[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if not after_clean["banned"] and after_clean["em_dashes"] == 0 else "partial",
            status="done",
        ))
        content.status = "published"
        db.add(RunLog(site_id=site.id, message=f"Applied content cleanup: {content.title}"))

    elif appr.kind == "required_page":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not content:
            return False, "no_connection"
        status = "publish" if publish else "draft"
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_page(content.title, content.body, status=status)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Page create failed for “{appr.title}”: {exc}"))
            db.commit()
            return False, "publish_fail"
        content.status = "published" if status == "publish" else "in_wordpress_draft"
        # A draft page still 404s for visitors and the crawler, so only clear the
        # finding when it's actually published live; a draft re-surfaces next audit.
        if publish:
            finding = db.get(Finding, payload.get("finding_id"))
            if finding:
                finding.status = "closed"
        db.add(RunLog(site_id=site.id,
                      message=f"Created page ({status}) in WordPress: {content.title} {result.get('link', '')}"))

    elif appr.kind == "website_css":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return False, "no_connection"
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            change.old_css = wp.get_custom_css()  # back up the actual current CSS now
            wp.update_custom_css(change.css)
        except WordPressError as exc:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Website change failed for “{appr.title}”: {exc}"))
            db.commit()
            return False, "publish_fail"
        _flush_cache(conn)
        change.status = "applied"
        db.add(RunLog(site_id=site.id, message=f"Applied website change: {change.request[:80]}"))

    elif appr.kind == "page_rewrite":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return False, "no_connection"
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        # Write the LIVE render source (_meridian_body), snapshotting it first for revert.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Page rewrite failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return False, "publish_fail"
        if live:
            change.old_css = live
        verified = write_body(client, page_id, change.css)
        if not verified:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Page rewrite write didn't verify for “{appr.title}”."))
            db.commit()
            return False, "publish_fail"
        change.status = "applied"
        db.add(FixRecord(
            site_id=site.id, doer="Elementor On-page",
            action_taken=f"Full-page SEO rewrite of “{appr.title}” (via _meridian_body)",
            page_ref=str(page_id), field="page_html",
            before_value=(change.old_css or "")[:5000], after_value=(change.css or "")[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed",
            status="done", outcome_pending=True,
        ))
        db.add(RunLog(
            site_id=site.id,
            message=f"Applied SEO page rewrite: {appr.title} ({'verified live' if verified else 'apply ok, verify pending'})",
        ))

    elif appr.kind == "schema_inject":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return False, "no_connection"
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        jstr = payload.get("jsonld", "")
        # Re-read the LIVE body and layer the schema onto CURRENT content so we don't
        # clobber other edits (e.g. image dimensions) made since proposing.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Schema injection failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return False, "publish_fail"
        from .schema_agent import _has_entity_schema
        change.old_css = live
        if jstr and not _has_entity_schema(live):
            change.css = live + f'\n<script type="application/ld+json">\n{jstr}\n</script>\n'
        else:
            change.css = live  # already has entity schema / nothing to add — no-op write
        if not write_body(client, page_id, change.css):
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Schema write didn't verify for “{appr.title}”."))
            db.commit()
            return False, "publish_fail"
        verified = bool(jstr)
        change.status = "applied"
        if verified:
            for f in db.query(Finding).filter(
                    Finding.site_id == site.id,
                    Finding.category.in_(("no_entity_schema", "no_localbusiness_schema")),
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
        db.add(FixRecord(
            site_id=site.id, doer="SEO Technical",
            action_taken="Injected homepage entity schema (via _meridian_body)",
            page_ref=str(page_id), field="schema",
            before_value="(no entity schema)", after_value=payload.get("jsonld", "")[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
        ))
        db.add(RunLog(site_id=site.id,
                      message=f"Applied homepage schema: {appr.title} "
                              f"({'verified live' if verified else 'apply ok, verify pending'})"))

    elif appr.kind == "img_dims":
        from .image_agent import _add_dims
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return False, "no_connection"
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        sizes = payload.get("sizes") or {}
        # Re-read the LIVE body and re-inject dimensions into CURRENT content,
        # snapshotting a true revert point.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Image-dimension fix failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return False, "publish_fail"
        change.old_css = live
        change.css = _add_dims(live, sizes) if sizes else live
        if not write_body(client, page_id, change.css):
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Image-dimension write didn't verify for “{appr.title}”."))
            db.commit()
            return False, "publish_fail"
        verified = True
        change.status = "applied"
        # Close the in-progress image-dimension finding(s); any still missing on
        # other pages re-detect on the next audit.
        for f in db.query(Finding).filter(
                Finding.site_id == site.id, Finding.category == "image_no_dimensions",
                Finding.status == "in-progress").all():
            f.status = "closed"
        db.add(FixRecord(
            site_id=site.id, doer="Website Agent",
            action_taken=f"Added image dimensions ({payload.get('count', '?')} images, via _meridian_body)",
            page_ref=str(page_id), field="image_no_dimensions",
            before_value="(no width/height)", after_value=f"{payload.get('count', '?')} images sized",
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
        ))
        db.add(RunLog(site_id=site.id,
                      message=f"Applied image dimensions: {appr.title} "
                              f"({'verified live' if verified else 'apply ok, verify pending'})"))

    appr.status = "approved"
    appr.decided_at = utcnow()
    db.commit()
    return True, "approved"


def reject_approval(db, appr: Approval) -> None:
    """Reject one pending approval (moved verbatim from the /reject route)."""
    try:
        payload = json.loads(appr.payload or "{}")
    except Exception:
        payload = {}
    if appr.kind in ("content", "required_page", "content_fix"):
        content = db.get(Content, payload.get("content_id")) if payload.get("content_id") else None
        if content:
            content.status = "rejected"
        if appr.kind == "required_page":
            finding = db.get(Finding, payload.get("finding_id")) if payload.get("finding_id") else None
            if finding:
                finding.status = "open"  # back to the queue
    elif appr.kind == "meta_rewrite":
        finding = db.get(Finding, payload.get("finding_id")) if payload.get("finding_id") else None
        if finding:
            finding.status = "open"
    elif appr.kind in ("website_css", "page_rewrite", "schema_inject", "img_dims"):
        change = db.get(SiteChange, payload.get("change_id")) if payload.get("change_id") else None
        if change:
            change.status = "rejected"
    appr.status = "rejected"
    appr.decided_at = utcnow()
    db.commit()
