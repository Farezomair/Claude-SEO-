"""Elementor On-page agent — full-page SEO rewrites via the Abilities API.

Every Meridian page is a single Elementor `html` widget holding a complete HTML
document (see docs/abilities-catalog.md). This agent reads that page body, has
Claude rewrite the visible copy for search intent while preserving structure,
CSS, and scripts, runs safety checks, and routes the change to the approval gate
with a saved snapshot for one-click rollback. Nothing touches the live page until
the owner approves — and approval/revert both go through `apply_html`.
"""
import json
import threading

from .abilities import AbilitiesClient, AbilitiesError, AbilitiesUnavailable
from .brain import rewrite_page_html
from .content_standard import scan, strip_em_dashes
from .database import SessionLocal
from .models import Approval, JobRun, RunLog, Site, SiteChange
from .rules import rules_for

P = "hostinger-ai-assistant"
A_FIND = f"{P}/elementor-find-widgets"
A_UPDATE_CONTENT = f"{P}/elementor-update-widget-content"
A_PAGE_GET = f"{P}/pages-get"
A_PAGE_UPDATE = f"{P}/pages-update"
A_LIST_PAGES = f"{P}/elementor-list-pages"
A_CACHE_FLUSH = f"{P}/litespeed-cache-flush"


# -- reads -------------------------------------------------------------------
def list_elementor_pages(conn: dict) -> list[dict]:
    """Published Elementor pages for the Website tab (best-effort)."""
    client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
    try:
        res = client.read(A_LIST_PAGES, {"post_type": "page", "post_status": "publish", "limit": 50})
        return (res or {}).get("pages") or []
    except (AbilitiesError, AbilitiesUnavailable):
        return []


def _find_html_widget(client: AbilitiesClient, page_id: int) -> tuple[str, str]:
    """Return (widget_id, current_html) for the page's html widget, or ('', '')."""
    res = client.read(A_FIND, {"post_id": page_id, "widget_types": ["html"], "include_settings": True})
    for w in (res or {}).get("widgets") or []:
        if w.get("widget_type") == "html":
            return w.get("id") or "", ((w.get("settings") or {}).get("html") or "")
    return "", ""


# -- writes (shared by approve + revert) -------------------------------------
def _set_widget_html(nodes, widget_id: str, html: str) -> bool:
    """Recursively set settings.html on the matching node in _elementor_data."""
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        if isinstance(n, dict):
            if n.get("id") == widget_id:
                n.setdefault("settings", {})["html"] = html
                return True
            if _set_widget_html(n.get("elements", []), widget_id, html):
                return True
    return False


def apply_html(client: AbilitiesClient, page_id: int, widget_id: str, html: str) -> str:
    """Write HTML into the page's html widget. Returns the method used.

    Tries the dedicated widget-content ability first (atomic, preserves other
    settings). If that ability rejects html widgets, falls back to a surgical
    edit of just this widget's node inside _elementor_data. Both are reversible
    via the saved snapshot. Best-effort cache flush after.
    """
    method = "widget-content"
    try:
        client.run(A_UPDATE_CONTENT, {"post_id": page_id, "widget_id": widget_id, "content": html})
    except AbilitiesError:
        method = "elementor-data"
        page = client.read(A_PAGE_GET, {"id": page_id})
        data_str = (((page or {}).get("meta") or {}).get("_elementor_data")) or ""
        if not data_str:
            raise AbilitiesError("Could not read _elementor_data to edit the page body.")
        data = json.loads(data_str)
        if not _set_widget_html(data, widget_id, html):
            raise AbilitiesError(f"Widget {widget_id} not found in the page's Elementor data.")
        client.run(A_PAGE_UPDATE, {"id": page_id, "meta": {"_elementor_data": json.dumps(data)}})
    try:
        client.run(A_CACHE_FLUSH, {})
    except (AbilitiesError, AbilitiesUnavailable):
        pass
    return method


def verify_html(client: AbilitiesClient, page_id: int, expected_html: str) -> bool:
    """Re-read the page's html widget and confirm the new content is live."""
    try:
        _wid, live = _find_html_widget(client, page_id)
    except (AbilitiesError, AbilitiesUnavailable):
        return False
    sample = (expected_html or "")[:400].strip()
    return bool(sample) and sample in (live or "")


# -- safety checks -----------------------------------------------------------
def validate_rewrite(old_html: str, new_html: str) -> list[str]:
    """Flag anything that suggests the rewrite could break the live page."""
    flags = []
    if not new_html or len(new_html) < 0.5 * len(old_html):
        flags.append("Rewrite looks truncated (much shorter than the original).")
    if "<style" in old_html and "<style" not in new_html:
        flags.append("Missing <style> block — page styling could break.")
    if "<script" in old_html and "<script" not in new_html:
        flags.append("Missing <script> block — interactive parts (e.g. FAQ toggles) could break.")
    old_links, new_links = old_html.count("href="), new_html.count("href=")
    if old_links and new_links < 0.7 * old_links:
        flags.append(f"Lost links ({new_links} vs {old_links} originally).")
    old_imgs, new_imgs = old_html.count("<img"), new_html.count("<img")
    if old_imgs and new_imgs < old_imgs:
        flags.append(f"Lost images ({new_imgs} vs {old_imgs} originally).")
    sc = scan(new_html)
    if sc.get("em_dashes"):
        flags.append("Contained em dashes (auto-stripped).")
    if sc.get("banned"):
        flags.append("Contains banned words: " + ", ".join(sc["banned"][:5]))
    return flags


# -- the run -----------------------------------------------------------------
def run_page_rewrite(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        if not client.available():
            run.status = "failed"
            run.summary = "The site's Abilities API is not reachable. Check the connection."
            db.commit()
            return

        try:
            widget_id, old_html = _find_html_widget(client, page_id)
        except (AbilitiesError, AbilitiesUnavailable) as exc:
            run.status = "failed"
            run.summary = f"Could not read the page: {exc}"
            db.commit()
            return
        if not widget_id or not old_html:
            run.status = "failed"
            run.summary = f"No editable HTML widget found on page {page_id}."
            db.commit()
            return

        try:
            result = rewrite_page_html(site.name, site.url, page_title or str(page_id),
                                       old_html, rules_for("shared", "website"))
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Rewrite generation failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return

        new_html = strip_em_dashes(result.get("html", ""))
        if not new_html:
            run.status = "failed"
            run.summary = "The model returned no HTML."
            db.commit()
            return

        flags = validate_rewrite(old_html, new_html)
        change = SiteChange(
            site_id=site_id, kind="page_rewrite",
            request=f"SEO rewrite: {page_title or ('page ' + str(page_id))}",
            css=new_html, old_css=old_html, status="proposed",
            target_page_id=page_id, target_widget_id=widget_id,
        )
        db.add(change)
        db.commit()
        db.refresh(change)

        summary = result.get("summary", "SEO rewrite of the page copy.")
        if flags:
            summary += "  ⚠ Review before approving: " + " ".join(flags)
        db.add(Approval(
            site_id=site_id, kind="page_rewrite",
            title=f"SEO rewrite: {page_title or ('page ' + str(page_id))}",
            summary=summary,
            payload=json.dumps({"change_id": change.id, "page_id": page_id,
                                "widget_id": widget_id, "flags": flags}),
            status="pending",
        ))
        run.status = "completed"
        run.summary = f"Proposed an SEO rewrite of '{page_title or page_id}' — waiting for your approval."
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


def start_page_rewrite_async(site_id: int, run_id: int, conn: dict,
                             page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_page_rewrite,
                     args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
