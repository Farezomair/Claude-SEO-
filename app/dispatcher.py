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
from .schema_agent import run_schema_inject
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
MAX_REWRITES = 3      # cap full-page rewrites per run (each is a big Claude call)

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
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
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


def _propose_required_page(db, ctx, f):
    page_type = _page_type(f.issue)
    if _pending_payload_match(db, ctx["site"].id, "required_page", "page_type", page_type):
        return ("in-progress", f"A {page_type} page draft is already waiting in Approvals.", False)
    try:
        page = generate_page(ctx["site"].name, ctx["site"].url, page_type)
    except Exception as exc:
        return ("escalated", f"Page draft failed ({exc.__class__.__name__}).", False)
    if not page.get("title") or not page.get("body_html"):
        return ("open", "Draft came back empty; will retry next run.", False)
    content = Content(site_id=ctx["site"].id, title=page["title"],
                      body=strip_em_dashes(page["body_html"]), status="draft")
    db.add(content)
    db.commit()
    db.refresh(content)
    summary = "New page draft — review before publishing."
    if page.get("legal"):
        summary += " Legal template — review with a professional and fill the [bracketed] placeholders."
    db.add(Approval(site_id=ctx["site"].id, kind="required_page",
                    title=f"Create {page_type} page: {page['title']}", summary=summary,
                    payload=json.dumps({"content_id": content.id, "finding_id": f.id, "page_type": page_type}),
                    status="pending"))
    return ("in-progress", f"Drafted a {page_type} page → sent to Approvals for your sign-off.", False)


def _propose_dedupe(db, ctx, f):
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("open", "Couldn't match the duplicate-title page in WordPress.", False)
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
    db.add(Approval(site_id=ctx["site"].id, kind="meta_rewrite",
                    title=f"Boost ranking page: {item['link']}",
                    summary=f"Stronger title/description to win more clicks. “{sugg.get('title','')}”.",
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
    run_schema_inject(ctx["site"].id, sj.id, ctx["conn"])
    ctx["schema_done"] = True
    made = db.query(Approval).filter(Approval.site_id == ctx["site"].id, Approval.kind == "schema_inject",
                                     Approval.status == "pending").count()
    if made:
        return ("in-progress", "Generated Organization/LocalBusiness schema → sent to Approvals.", False)
    return ("open", "Couldn't generate schema this run (will retry).", False)


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
    if ctx["rewrites_used"] >= MAX_REWRITES:
        return ("open", "Queued — full-page rewrite cap reached this run; will continue next run.", False)
    from .elementor_agent import run_page_rewrite
    sub = JobRun(site_id=ctx["site"].id, kind="elementor", status="running",
                 summary=f"Rewriting {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    run_page_rewrite(ctx["site"].id, sub.id, ctx["conn"], pid, title)
    if _pending_payload_match(db, ctx["site"].id, "page_rewrite", "page_id", pid):
        ctx["rewrite_pages"].add(pid)
        ctx["rewrites_used"] += 1
        return ("in-progress",
                "Proposed a full-page SEO rewrite (adds FAQ, quotable lead answers, tables, "
                "fixes heading order, deepens content) → sent to Approvals.", False)
    return ("no-capability", f"Couldn't rewrite this page: {sub.summary}", False)


def _handle_broken(db, ctx, f):
    """Broken link/page: required-page 404s are covered by the page drafts; external
    bot-blocks are not real defects; otherwise we have no redirects doer yet."""
    issue = (f.issue or "").lower()
    path = urlparse(f.evidence_url or "").path.lower()
    kw = next((k for k in REQUIRED_KEYWORDS if k in path), None)
    if kw:
        kw = "terms" if kw == "tos" else kw
        return ("no-capability",
                f"This 404 is the missing {kw} page — it clears once the {kw} page draft "
                f"in Approvals is published.", False)
    if "blocks automated checks" in issue or "403" in issue:
        return ("no-capability",
                "External link that blocks bots; it works for real visitors — no action needed.", False)
    return ("no-capability", NO_CAP.get(f.category, "Broken link — needs a redirects doer (not built yet)."), False)


def _propose_img_dims(db, ctx, f):
    """Measure images on the page and propose width/height to stop layout shift."""
    item = (ctx.get("items") or {}).get(_norm(f.evidence_url))
    if not item:
        return ("no-capability", "Couldn't match this page to fix its image dimensions.", False)
    pid, title = item["id"], item.get("title", "")
    if _pending_payload_match(db, ctx["site"].id, "img_dims", "page_id", pid):
        return ("in-progress", "An image-dimensions fix for this page is already in Approvals.", False)
    from .image_agent import run_image_dims
    sub = JobRun(site_id=ctx["site"].id, kind="image", status="running",
                 summary=f"Measuring images on {title or pid}…")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    run_image_dims(ctx["site"].id, sub.id, ctx["conn"], pid, title)
    if _pending_payload_match(db, ctx["site"].id, "img_dims", "page_id", pid):
        return ("in-progress", "Measured the images and proposed width/height (stops layout shift) → Approvals.", False)
    return ("no-capability", f"Couldn't fix images: {sub.summary}", False)


HANDLERS = {
    **{c: _fix_meta for c in META_CATS},
    **{c: _propose_rewrite for c in REWRITE_CATS},
    "image_no_dimensions": _propose_img_dims,
    "required_page_missing": _propose_required_page,
    "duplicate_title": _propose_dedupe,
    "striking_distance": _propose_ranking,
    "low_ctr": _propose_ranking,
    "no_entity_schema": _propose_schema,
    "no_localbusiness_schema": _propose_schema,
    "broken_link": _handle_broken,
    "broken_page": _handle_broken,
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

        ctx = {"site": site, "conn": conn, "auto_used": 0, "rewrites_used": 0, "flush": False,
               "schema_done": False, "rewrite_pages": set(),
               "wp": WordPressClient(conn["url"], conn["username"], conn["app_password"]),
               "items": None}
        # Build the URL->page map once if any finding needs a page lookup.
        lookup_cats = META_CATS | REWRITE_CATS | {"duplicate_title", "striking_distance", "low_ctr", "image_no_dimensions"}
        if any(f.category in lookup_cats for f in findings):
            try:
                ctx["items"] = {_norm(it["link"]): it for it in ctx["wp"].list_content(limit=100) if it.get("link")}
            except Exception:
                ctx["items"] = {}

        for i, f in enumerate(findings, start=1):
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
            if run:
                run.status = "completed"
                run.summary = result["summary"]
                run.fixes_count = result["auto"]
                db.commit()
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True).start()
