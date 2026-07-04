"""The Dispatcher — works through the audit's findings ONE BY ONE.

After an audit, this walks each open finding in priority order and, for that single
finding, does exactly one of three things and records a per-line remark on the
finding so the owner can see what happened:

  • FIX    — the doer can do it itself (e.g. write a page's meta title/description).
             Applied to the live site, verified, cache-flushed. status -> closed.
  • PROPOSE — needs human sign-off (new page, schema, title rewrite). A draft/change
             goes to Approvals. status -> in-progress, remark says it's waiting.
  • NO CAPABILITY — no doer for this yet. status -> no-capability, remark explains
             what is needed (and points to the manual tool where one exists).

Progress is reported on the run (progress_done/total + the current finding label) so
the Command Center can show a bar filling as the crew works through the list.
Doers are grounded in the harvested skill knowledge (see knowledge.py / brain.py).
"""
import json
import threading
from urllib.parse import urlparse

from .brain import generate_meta, generate_page, improve_meta
from .connections import get_connection
from .content_standard import strip_em_dashes
from .database import SessionLocal
from .models import Approval, Content, Finding, FixRecord, JobRun, RunLog, Site
from .schema_agent import start_schema_inject_async
from .website_agent import _page_type
from .wordpress import YOAST_DESC_KEY, YOAST_TITLE_KEY, WordPressClient, WordPressError

SEVERITY_RANK = {"blocker": 0, "critical": 1, "high": 2, "medium": 3, "low": 4}
META_CATS = {"meta_title", "meta_description", "missing_title", "title_length", "meta_description_missing"}
# Findings that a full-page Elementor rewrite addresses (FAQ, quotable answers,
# tables, deeper content, heading structure) — grounded in the EEAT+GEO knowledge.
REWRITE_CATS = {"thin_content", "eeat_weak", "content_shallow", "content_stale",
                "geo_unstructured", "heading_hierarchy", "missing_h1", "multiple_h1", "nap_missing"}
REQUIRED_KEYWORDS = ("privacy", "terms", "tos", "about", "contact", "accessibility")
MAX_AUTO_FIXES = 25   # cap live auto-writes per run (each is a Claude call)
MAX_REWRITES = 20     # cap full-page rewrites per run; a semaphore bounds concurrency

# Honest "we can't auto-fix this yet" messages, per category. Points at the manual
# tool where one already exists.
NO_CAP = {
    "broken_link": "Broken link — removing/redirecting it needs a redirects doer (not built yet).",
    "broken_page": "Page returns an error — restoring or redirecting it needs your review.",
    "redirect_issue": "Redirect cleanup needs a redirects doer (not built yet).",
    "security_headers": "Security headers are set at the host/CDN or via a headers plugin (no doer yet).",
    "no_https": "HTTPS enforcement is a host/server setting (no doer).",
    "mixed_content": "Mixed-content fixes need per-asset edits (no doer yet).",
    "ai_crawler_blocked": "Editing robots.txt needs a robots doer (not built yet).",
    "no_llms_txt": "Creating llms.txt needs a file-write doer (not built yet).",
    "missing_h1": "Heading fixes need the page-content doer — use 'Rewrite for SEO' on this page.",
    "multiple_h1": "Heading fixes need the page-content doer — use 'Rewrite for SEO' on this page.",
    "heading_hierarchy": "Heading-structure fixes need the page-content doer — use 'Rewrite for SEO'.",
    "missing_viewport": "Viewport is a theme/template setting (no doer yet).",
    "missing_favicon": "Adding a favicon needs a media/theme doer (not built yet).",
    "images_missing_alt": "Alt-text doer not built yet (next on the roadmap).",
    "image_no_dimensions": "Image-dimension fixes need an image doer (not built yet).",
    "image_legacy_format": "WebP/AVIF conversion needs an image doer (not built yet).",
    "missing_canonical": "Canonical fixes need a head/SEO-plugin doer (not built yet).",
    "indexation": "Index directive — flagged for your review.",
    "structure": "Header/footer structure is theme-level (often a false positive on page builders).",
    "og_incomplete": "Open Graph tags need an OG doer (not built yet).",
    "cwv_poor": "Performance needs a LiteSpeed/performance doer (not built yet).",
    "nap_missing": "Adding phone/NAP needs a content edit — use 'Rewrite for SEO' on the homepage.",
    "thin_content": "Thin content needs expansion — use 'Rewrite for SEO' on this page.",
    "eeat_weak": "E-E-A-T needs a content rewrite — use 'Rewrite for SEO' on this page.",
    "content_shallow": "Needs deeper content — use 'Rewrite for SEO' on this page.",
    "content_stale": "Needs a freshness update — use 'Rewrite for SEO' on this page.",
    "schema_deprecated": "Removing deprecated FAQPage/HowTo schema needs a schema-cleanup doer (not built yet).",
    "schema_invalid": "Invalid structured data needs manual review.",
    "schema_placeholder": "Schema contains placeholder text — needs manual review.",
    "missing_schema": "Use the homepage schema proposal (entity schema) — covered by the schema doer.",
    "orphan_page": "Linking an orphan page needs an internal-links doer (not built yet).",
}


def _norm(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc.lower().removeprefix("www.") + (p.path or "/").rstrip("/")).lower()


def _pending_payload_match(db, site_id, kind, key, value) -> bool:
    for a in (db.query(Approval).filter(Approval.site_id == site_id, Approval.kind == kind,
                                        Approval.status == "pending").all()):
        try:
            if json.loads(a.payload or "{}").get(key) == value:
                return True
        except Exception:
            pass
    return False


# ---- per-finding handlers: return (new_status, remark, applied_bool) ----
def _fix_meta(db, ctx, f):
    items = ctx.get("items") or {}
    item = items.get(_norm(f.evidence_url))
    if not item:  # fallback: match by URL path only (host/scheme/www differences)
        path = (urlparse(f.evidence_url or "").path or "/").rstrip("/").lower()
        item = (ctx.get("items_by_path") or {}).get(path)
    if not item:
        return ("open", "Couldn't match this URL to a WordPress page to edit its meta.", False)
    if ctx["auto_used"] >= MAX_AUTO_FIXES:
        return ("open", "Queued — per-run fix cap reached; will continue next run.", False)
    try:
        sugg = generate_meta(item["title"], item["link"], item["content_text"], ctx["site"].name)
    except Exception as exc:
        return ("escalated", f"Meta generation failed ({exc.__class__.__name__}).", False)

    want_title = f.category in ("meta_title", "missing_title", "title_length")
    want_desc = f.category in ("meta_description", "meta_description_missing")
    meta, parts = {}, []
    if (want_title or not (want_title or want_desc)) and sugg.get("title"):
        meta[YOAST_TITLE_KEY] = sugg["title"]
        parts.append(f"title → “{sugg['title']}”")
    if (want_desc or not (want_title or want_desc)) and sugg.get("description"):
        meta[YOAST_DESC_KEY] = sugg["description"]
        parts.append("description written")
    if not meta:
        return ("open", "Nothing to write (model returned empty meta).", False)
    try:
        ctx["wp"].update_meta(item["kind"], item["id"], meta)
        live = ctx["wp"].get_meta(item["kind"], item["id"])
    except WordPressError as exc:
        return ("escalated", f"WordPress rejected the write: {exc}", False)
    ok = all((live.get(k) or "").strip() == v.strip() for k, v in meta.items())
    ctx["flush"] = True
    ctx["auto_used"] += 1
    _fixrecord(db, ctx, f, "SEO Technical", "Set meta: " + "; ".join(parts),
               after=json.dumps(meta)[:500], verified=ok, method="auto-safe")
    return ("closed" if ok else "reopened",
            ("Fixed — " + "; ".join(parts)) if ok else "Wrote meta but couldn't verify it live (cache?).",
            True)


_PUBLISH_INSTR = (
    "This page will be PUBLISHED immediately. Write complete, professional, "
    "publication-ready content with NO visible [bracketed] placeholders. If it is a "
    "legal page (privacy/terms), open with a one-line notice that this is a general "
    "template the owner should review with a professional, then write sensible generic "
    "terms — never invent specific facts, addresses, or numbers.")


def _ensure_footer_link_doer(db, ctx):
    """Fire the internal-linking doer once per run — it links every orphaned
    required page (published but reachable from no internal link) into the footer,
    verifies it live, and closes the finding. Orphaned pages are invisible to the
    audit, to Google, and to visitors, so publishing alone never clears them."""
    if ctx.get("link_done"):
        return
    from .link_agent import start_footer_links_async
    lj = JobRun(site_id=ctx["site"].id, kind="linking", status="running",
                summary="Linking orphaned pages into the footer…")
    db.add(lj)
    db.commit()
    db.refresh(lj)
    start_footer_links_async(ctx["site"].id, lj.id, ctx["conn"])
    ctx["link_done"] = True


def _propose_required_page(db, ctx, f):
    """Make the required page exist AND be reachable. If it isn't published yet,
    create + publish it at its conventional slug; if it exists but is orphaned
    (the audit flags it because no internal link reaches it), hand off to the
    internal-linking doer to add a footer link. Either way the finding only closes
    once the page is live AND linked (the linking doer verifies + closes it)."""
    page_type = _page_type(f.issue)
    if "existing_slugs" not in ctx:
        try:
            ctx["existing_slugs"] = ctx["wp"].page_slugs()
        except Exception:
            ctx["existing_slugs"] = set()
    published = ctx.setdefault("pages_published", set())
    created_note = ""
    if page_type not in published and page_type not in ctx["existing_slugs"]:
        try:
            page = generate_page(ctx["site"].name, ctx["site"].url, page_type, instructions=_PUBLISH_INSTR)
        except Exception as exc:
            return ("escalated", f"Page generation failed ({exc.__class__.__name__}).", False)
        if not page.get("title") or not page.get("body_html"):
            return ("open", "Page came back empty; will retry next run.", False)
        try:
            result = ctx["wp"].create_page(page["title"], strip_em_dashes(page["body_html"]),
                                           status="publish", slug=page_type)
        except WordPressError as exc:
            return ("escalated", f"Publishing the {page_type} page failed: {exc}", False)
        published.add(page_type)
        ctx["existing_slugs"].add(result.get("slug") or page_type)
        _fixrecord(db, ctx, f, "Website Agent", f"Created + published the {page_type} page",
                   after=(result.get("link", "") or f"/{page_type}")[:500], method="auto-safe")
        created_note = f"Published the {page_type} page. "
    # The page exists now (just created, or already there but orphaned). Link it
    # into the footer so the crawler/Google/visitors can actually reach it.
    _ensure_footer_link_doer(db, ctx)
    return ("in-progress",
            created_note + f"Linking the {page_type} page into the footer so the audit, Google, and "
            "visitors can find it — auto, verified live, then this finding closes.", False)


def _propose_dedupe(db, ctx, f):
    items = ctx.get("items") or {}
    item = items.get(_norm(f.evidence_url))
    if not item:
        # Fall back to matching the duplicated title text against known pages/posts.
        import re as _re
        m = _re.search(r'"(.+?)"', f.issue or "")
        if m:
            t = m.group(1).strip().lower()
            item = next((it for it in items.values() if (it.get("title") or "").strip().lower() == t), None)
    if not item:
        return ("no-capability",
                "These duplicate titles are on pages not editable via WordPress "
                "(e.g. static blog cards) — needs manual review.", False)
    if _pending_payload_match(db, ctx["site"].id, "meta_rewrite", "page_id", item["id"]):
        return ("in-progress", "A unique-title rewrite is already waiting in Approvals.", False)
    try:
        sugg = generate_meta(item["title"], item["link"], item["content_text"], ctx["site"].name)
    except Exception as exc:
        return ("escalated", f"Title generation failed ({exc.__class__.__name__}).", False)
    new_title = sugg.get("title", "")
    old_title = (item["meta"].get(YOAST_TITLE_KEY) or item.get("title") or "").strip()
    if not new_title or new_title.strip().lower() == old_title.lower():
        return ("open", "No better unique title found.", False)
    db.add(Approval(site_id=ctx["site"].id, kind="meta_rewrite",
                    title=f"Make title unique: {item['link']}",
                    summary=f"Duplicate title. “{old_title}” → “{new_title}”.",
                    payload=json.dumps({"finding_id": f.id, "page_kind": item["kind"], "page_id": item["id"],
                                        "new_title": new_title, "new_desc": "", "old_title": old_title, "old_desc": ""}),
                    status="pending"))
    return ("in-progress", f"Proposed a unique title → sent to Approvals.", False)


def _propose_ranking(db, ctx, f):
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("open", "Couldn't match this ranking page in WordPress.", False)
    if _pending_payload_match(db, ctx["site"].id, "meta_rewrite", "page_id", item["id"]):
        return ("in-progress", "A ranking rewrite for this page is already in Approvals.", False)
    cur_title = (item["meta"].get(YOAST_TITLE_KEY) or item.get("title") or "").strip()
    cur_desc = (item["meta"].get(YOAST_DESC_KEY) or "").strip()
    try:
        sugg = improve_meta(item["title"], item["link"], item["content_text"], cur_title, cur_desc,
                            site_name=ctx["site"].name)
    except Exception as exc:
        return ("escalated", f"Rewrite generation failed ({exc.__class__.__name__}).", False)
    if not sugg.get("title") and not sugg.get("description"):
        return ("open", "No stronger title/description found.", False)
    # GA4 enrichment (enhance-bar toggle): show the page's real organic traffic so
    # the owner can judge how much this ranking win is worth.
    ga4_note = ""
    from .capabilities import cap_setting
    if cap_setting("ranking", "use_ga4", False):
        if "ga4_pages" not in ctx:
            from .ga4 import organic_sessions_by_page
            ctx["ga4_pages"] = organic_sessions_by_page()
        if ctx["ga4_pages"]:
            path = urlparse(item["link"]).path.rstrip("/") or "/"
            ga4_note = f" GA4: ~{ctx['ga4_pages'].get(path, 0)} organic visits in the last 28 days."
        else:
            ga4_note = " GA4: enabled but no data — reconnect Google to grant the Analytics permission."
    db.add(Approval(site_id=ctx["site"].id, kind="meta_rewrite",
                    title=f"Boost ranking page: {item['link']}",
                    summary=f"Stronger title/description to win more clicks. “{sugg.get('title','')}”.{ga4_note}",
                    payload=json.dumps({"finding_id": f.id, "page_kind": item["kind"], "page_id": item["id"],
                                        "new_title": sugg.get("title", ""), "new_desc": sugg.get("description", ""),
                                        "old_title": cur_title, "old_desc": cur_desc}),
                    status="pending"))
    return ("in-progress", "Proposed a stronger title/description → sent to Approvals.", False)


def _propose_schema(db, ctx, f):
    if _pending_payload_match(db, ctx["site"].id, "schema_inject", "page_id", None) or (
            db.query(Approval).filter(Approval.site_id == ctx["site"].id, Approval.kind == "schema_inject",
                                      Approval.status == "pending").count()):
        return ("in-progress", "Homepage schema is already waiting in Approvals.", False)
    if ctx.get("schema_done"):
        return ("in-progress", "Covered by the homepage schema proposal.", False)
    sj = JobRun(site_id=ctx["site"].id, kind="schema", status="running", summary="Generating homepage schema…")
    db.add(sj)
    db.commit()
    db.refresh(sj)
    start_schema_inject_async(ctx["site"].id, sj.id, ctx["conn"])  # background
    ctx["schema_done"] = True
    return ("in-progress", "Generating Organization/LocalBusiness schema in the background → Approvals.", False)


def _propose_technical(db, ctx, f):
    """Server-level technical fixes (security headers, llms.txt) via the Bridge
    plugin — auto-applied + verified live, once per run."""
    if ctx.get("tech_done"):
        return ("in-progress", "Covered by the technical fix running this run (security headers + llms.txt).", False)
    from .technical_agent import start_technical_async
    tj = JobRun(site_id=ctx["site"].id, kind="technical", status="running", summary="Applying technical fixes…")
    db.add(tj)
    db.commit()
    db.refresh(tj)
    start_technical_async(ctx["site"].id, tj.id, ctx["conn"])  # background, auto-applies + verifies
    ctx["tech_done"] = True
    return ("in-progress",
            "Applying server-level technical fixes (security headers + llms.txt) — auto, verified live, reversible.", False)


def _propose_rewrite(db, ctx, f):
    """Propose ONE full-page SEO rewrite per page — fixes FAQ/quotable answers/
    tables/depth/headings together. Reuses the existing Elementor rewrite doer."""
    items = ctx.get("items") or {}
    item = items.get(_norm(f.evidence_url))
    if not item:
        return ("no-capability",
                "Needs a content rewrite, but this URL isn't an editable Elementor page.", False)
    pid, title = item["id"], item.get("title", "")
    if pid in ctx["rewrite_pages"]:
        return ("in-progress", "Covered by the full-page SEO rewrite proposed for this page (in Approvals).", False)
    if _pending_payload_match(db, ctx["site"].id, "page_rewrite", "page_id", pid):
        ctx["rewrite_pages"].add(pid)
        return ("in-progress", "A full-page SEO rewrite for this page is already waiting in Approvals.", False)
    if ctx["rewrites_used"] >= ctx.get("max_rewrites", MAX_REWRITES):
        return ("open", "Queued — full-page rewrite cap reached this run; will continue next run.", False)
    from .elementor_agent import start_page_rewrite_async
    sub = JobRun(site_id=ctx["site"].id, kind="elementor", status="running",
                 summary=f"Rewriting {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_page_rewrite_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background — don't block the bar
    ctx["rewrite_pages"].add(pid)
    ctx["rewrites_used"] += 1
    return ("in-progress",
            "Generating a full-page SEO rewrite (FAQ, quotable answers, tables, heading order, depth) in the "
            "background — auto-applies to the live page if the safety checks pass, otherwise lands in Approvals.", False)


def _handle_broken(db, ctx, f):
    """Broken link/page: required-page 404s are covered by the page drafts; external
    bot-blocks are not real defects; otherwise we have no redirects doer yet."""
    issue = (f.issue or "").lower()
    path = urlparse(f.evidence_url or "").path.lower()
    kw = next((k for k in REQUIRED_KEYWORDS if k in path), None)
    if kw:
        kw = "terms" if kw == "tos" else kw
        return ("in-progress",
                f"This 404 is the missing {kw} page — the {kw} page is being auto-created and "
                f"published, so this link resolves on the next audit.", False)
    if "blocks automated checks" in issue or "403" in issue:
        return ("no-capability",
                "External link that blocks bots; it works for real visitors — no action needed.", False)
    return ("no-capability", NO_CAP.get(f.category, "Broken link — needs a redirects doer (not built yet)."), False)


def _propose_redirects(db, ctx, f):
    """Broken internal link/page -> 301 redirect to the best live page (fires the
    redirects doer once per run). External bot-blockers aren't real defects."""
    issue = (f.issue or "").lower()
    if "blocks automated checks" in issue or "403" in issue:
        return ("no-capability",
                "External link that blocks bots; it works for real visitors — no action needed.", False)
    if not ctx.get("redirects_done"):
        from .redirect_agent import start_redirects_async
        rj = JobRun(site_id=ctx["site"].id, kind="redirects", status="running",
                    summary="Redirecting dead URLs…")
        db.add(rj)
        db.commit()
        db.refresh(rj)
        start_redirects_async(ctx["site"].id, rj.id, ctx["conn"])
        ctx["redirects_done"] = True
    return ("in-progress",
            "Redirecting broken internal URLs to the best live page (301, verified live) — "
            "external links left for your review.", False)


def _propose_img_dims(db, ctx, f):
    """Measure images on the page and propose width/height to stop layout shift."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match this page to fix its image dimensions.", False)
    pid, title = item["id"], item.get("title", "")
    from .image_agent import start_image_dims_async
    sub = JobRun(site_id=ctx["site"].id, kind="image", status="running",
                 summary=f"Measuring images on {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_image_dims_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background, auto-applies
    return ("in-progress", "Measuring the images and auto-adding width/height to the live page (invisible, revertible).", False)


def _propose_alt(db, ctx, f):
    """Write descriptive alt text for images missing it (auto-applied + verified)."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match this page to write image alt text.", False)
    pid, title = item["id"], item.get("title", "")
    from .alt_agent import start_alt_text_async
    sub = JobRun(site_id=ctx["site"].id, kind="alttext", status="running",
                 summary=f"Writing alt text on {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_alt_text_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background, auto-applies
    return ("in-progress", "Writing descriptive alt text for the images and auto-applying to the live page (verified).", False)


def _propose_headmeta(db, ctx, f):
    """Canonical / Open Graph / viewport / favicon via the Bridge head-injection
    (fires once per run; site-level toggles, verified live)."""
    if ctx.get("headmeta_done"):
        return ("in-progress", "Covered by the head/meta fix running this run.", False)
    from .headmeta_agent import start_headmeta_async
    hj = JobRun(site_id=ctx["site"].id, kind="headmeta", status="running", summary="Adding head tags…")
    db.add(hj)
    db.commit()
    db.refresh(hj)
    start_headmeta_async(ctx["site"].id, hj.id, ctx["conn"])
    ctx["headmeta_done"] = True
    return ("in-progress",
            "Adding the missing head tags (canonical / Open Graph / viewport / favicon) — auto, verified live.", False)


def _propose_robots(db, ctx, f):
    """Unblock AI crawlers in robots.txt (fires once per run, verified live)."""
    if ctx.get("robots_done"):
        return ("in-progress", "Covered by the robots.txt fix running this run.", False)
    from .robots_agent import start_robots_async
    rj = JobRun(site_id=ctx["site"].id, kind="robots", status="running", summary="Unblocking AI crawlers…")
    db.add(rj)
    db.commit()
    db.refresh(rj)
    start_robots_async(ctx["site"].id, rj.id, ctx["conn"])
    ctx["robots_done"] = True
    return ("in-progress",
            "Removing the robots.txt block on AI crawlers (GPTBot, ClaudeBot, …) — auto, verified live.", False)


def _propose_schema_cleanup(db, ctx, f):
    """Remove broken/placeholder/deprecated JSON-LD from the page (auto, verified)."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match this page to clean its schema.", False)
    pid, title = item["id"], item.get("title", "")
    from .schema_cleanup_agent import start_schema_cleanup_async
    sub = JobRun(site_id=ctx["site"].id, kind="schemaclean", status="running",
                 summary=f"Cleaning schema on {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_schema_cleanup_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background, auto-applies
    return ("in-progress",
            "Removing broken/placeholder/deprecated structured data from the live page (verified).", False)


def _propose_webp(db, ctx, f):
    """Serve the page's images as WebP/AVIF (CDN auto=format or convert+rehost)."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match this page to modernize its images.", False)
    pid, title = item["id"], item.get("title", "")
    from .webp_agent import start_webp_async
    sub = JobRun(site_id=ctx["site"].id, kind="webp", status="running",
                 summary=f"Modernizing images on {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_webp_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background, auto-applies
    return ("in-progress",
            "Switching the images to WebP/AVIF (CDN format negotiation, or convert + rehost) — verified live.", False)


def _propose_performance(db, ctx, f):
    """Lazy-load offscreen images (safe CWV win) on the measured page. CWV field
    data lags ~28 days, so this applies + records but doesn't claim an instant fix —
    the finding clears when the audit re-measures."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match the measured page to optimize it.", False)
    pid, title = item["id"], item.get("title", "")
    from .perf_agent import start_perf_async
    sub = JobRun(site_id=ctx["site"].id, kind="perf", status="running", summary=f"Optimizing {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    start_perf_async(ctx["site"].id, sub.id, ctx["conn"], pid, title)  # background, auto-applies
    return ("in-progress",
            "Lazy-loading offscreen images to improve Core Web Vitals — applied live; "
            "CWV field data reflects it over ~4 weeks.", False)


def _human_task(db, ctx, f):
    """Owner-only fact (real phone/license/prices). Can't be AI-fixed — surface it
    in Approvals under 'Needs your attention'. Recurs each audit until fixed."""
    return ("needs-human",
            "Requires your input — see “Needs your attention” in Approvals. "
            "Keeps reappearing each audit until fixed on the site.", False)


HANDLERS = {
    **{c: _fix_meta for c in META_CATS},
    **{c: _propose_rewrite for c in REWRITE_CATS},
    "needs_real_data": _human_task,
    "image_no_dimensions": _propose_img_dims,
    "images_missing_alt": _propose_alt,
    "missing_canonical": _propose_headmeta,
    "og_incomplete": _propose_headmeta,
    "missing_viewport": _propose_headmeta,
    "missing_favicon": _propose_headmeta,
    "schema_invalid": _propose_schema_cleanup,
    "schema_placeholder": _propose_schema_cleanup,
    "schema_deprecated": _propose_schema_cleanup,
    "ai_crawler_blocked": _propose_robots,
    "cwv_poor": _propose_performance,
    "image_legacy_format": _propose_webp,
    "required_page_missing": _propose_required_page,
    "duplicate_title": _propose_dedupe,
    "striking_distance": _propose_ranking,
    "low_ctr": _propose_ranking,
    "no_entity_schema": _propose_schema,
    "no_localbusiness_schema": _propose_schema,
    "security_headers": _propose_technical,
    "no_llms_txt": _propose_technical,
    "broken_link": _propose_redirects,
    "broken_page": _propose_redirects,
}


def _fixrecord(db, ctx, f, doer, action, after="", before="", verified=True, method="gate-approved"):
    db.add(FixRecord(
        site_id=ctx["site"].id, finding_id=f.id, doer=doer, action_taken=action,
        page_ref=f.evidence_url or "", field=f.category, before_value=before[:5000], after_value=after[:5000],
        method=method, lane="autonomous" if method == "auto-safe" else "gated", applied=True,
        verification_verdict="verified" if verified else "not_fixed", status="done",
    ))


def _set_progress(db, run, done, total, label):
    if not run:
        return
    run.progress_done, run.progress_total, run.progress_label = done, total, label
    db.commit()


def dispatch_fixes(site_id: int, progress_run_id: int | None = None) -> dict:
    """Walk the site's open findings one by one; fix / propose / flag each."""
    db = SessionLocal()
    fixed = proposed = no_cap = 0
    try:
        site = db.get(Site, site_id)
        conn = get_connection(site_id, site.url, site.name)
        run = db.get(JobRun, progress_run_id) if progress_run_id else None
        if not conn:
            if run:
                run.progress_label = "No WordPress connection — connect one in Settings."
                db.commit()
            return {"auto": 0, "proposed": 0, "no_cap": 0,
                    "summary": "No WordPress connection — connect one in Settings to apply fixes."}

        findings = (
            db.query(Finding)
            .filter(Finding.site_id == site_id, Finding.status == "open")
            .all()
        )
        findings.sort(key=lambda x: (SEVERITY_RANK.get(x.severity, 5), x.category))
        total = len(findings)

        from .capabilities import cap_setting
        ctx = {"site": site, "conn": conn, "auto_used": 0, "rewrites_used": 0, "flush": False,
               "schema_done": False, "rewrite_pages": set(),
               "max_rewrites": int(cap_setting("elementor", "max_rewrites_per_run", MAX_REWRITES)),
               "wp": WordPressClient(conn["url"], conn["username"], conn["app_password"]),
               "items": None}
        # Build the URL->page map once if any finding needs a page lookup.
        lookup_cats = META_CATS | REWRITE_CATS | {"duplicate_title", "striking_distance", "low_ctr",
                                                  "image_no_dimensions", "images_missing_alt", "cwv_poor",
                                                  "image_legacy_format",
                                                  "schema_invalid", "schema_placeholder", "schema_deprecated"}
        if any(f.category in lookup_cats for f in findings):
            try:
                _all = [it for it in ctx["wp"].list_content(limit=100) if it.get("link")]
                ctx["items"] = {_norm(it["link"]): it for it in _all}
                ctx["items_by_path"] = {(urlparse(it["link"]).path or "/").rstrip("/").lower(): it for it in _all}
            except Exception:
                ctx["items"], ctx["items_by_path"] = {}, {}

        for i, f in enumerate(findings, start=1):
            # Honor a Stop: if the run was cancelled, halt cleanly.
            if run:
                db.refresh(run)
                if run.status == "cancelled":
                    db.add(RunLog(site_id=site_id, message=f"Dispatch stopped at finding {i} of {total}."))
                    db.commit()
                    return {"auto": fixed, "proposed": proposed, "no_cap": no_cap,
                            "summary": f"Stopped at {i}/{total}: {fixed} fixed, {proposed} queued."}
            _set_progress(db, run, i - 1, total, f"{f.category.replace('_', ' ')} — {(f.evidence_url or '')[:60]}")
            handler = HANDLERS.get(f.category)
            try:
                if handler:
                    status, remark, applied = handler(db, ctx, f)
                else:
                    status, remark, applied = ("no-capability",
                                               NO_CAP.get(f.category, "No automated fixer for this yet."), False)
            except Exception as exc:
                status, remark, applied = ("escalated", f"Doer error: {exc.__class__.__name__}: {exc}", False)
            f.status = status
            f.remark = remark
            db.commit()
            if applied:
                fixed += 1
            elif status == "in-progress":
                proposed += 1
            elif status == "no-capability":
                no_cap += 1

        if ctx["flush"]:
            try:
                from .abilities import AbilitiesClient
                AbilitiesClient(conn["url"], conn["username"], conn["app_password"]).run(
                    "hostinger-ai-assistant/litespeed-cache-flush", {})
            except Exception:
                pass

        _set_progress(db, run, total, total, "Done")
        summary = (f"Worked {total} finding(s): {fixed} fixed, {proposed} sent to Approvals, "
                   f"{no_cap} need a doer we haven't built yet.")
        db.add(RunLog(site_id=site_id, message=f"Dispatcher: {summary}"))
        db.commit()
        return {"auto": fixed, "proposed": proposed, "no_cap": no_cap, "summary": summary}
    except Exception as exc:
        return {"auto": fixed, "proposed": proposed, "no_cap": no_cap,
                "summary": f"Dispatch failed: {exc.__class__.__name__}: {exc}"}
    finally:
        db.close()


def start_dispatch_async(site_id: int, run_id: int) -> None:
    def _run():
        result = dispatch_fixes(site_id, progress_run_id=run_id)
        db = SessionLocal()
        try:
            run = db.get(JobRun, run_id)
            if run and run.status != "cancelled":
                run.status = "completed"
                run.summary = result["summary"]
                run.fixes_count = result["auto"]
                db.commit()
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True).start()
