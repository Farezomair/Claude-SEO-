"""Contextual internal-linking doer — in-body links between related pages.

Descriptive in-content links are one of the highest-ROI things an SEO expert
does by hand: they pass authority to the pages that need it, tell Google what
the target page is about (anchor text), and keep visitors moving. This doer asks
Claude to pick natural link opportunities — an exact phrase already in the
page's copy that should link to another page on the site (preferring each
target's mapped keyword as anchor) — then injects the <a> with a byte-preserving
replacement and verifies it landed.

Safety: only links phrases that exist verbatim in visible copy, never inside an
existing <a>/heading/script/style, max 3 new links per page, never self-links,
skips targets the body already links. Reversible via the SiteChange snapshot.
"""
import json
import re
import threading

from bs4 import BeautifulSoup

from .brain import _extract_json, _get_client, ANTHROPIC_MODEL
from .database import SessionLocal
from .elementor_agent import AbilitiesClient, read_body, write_body
from .models import Finding, FixRecord, JobRun, KeywordTarget, RunLog, Site, SiteChange

MAX_LINKS_PER_PAGE = 3


def pick_link_spots(site_name: str, page_title: str, visible_text: str, targets: list) -> list:
    """Ask Claude for [{phrase, target_path}] — `phrase` must appear VERBATIM in
    the page's visible copy and read naturally as a link to `target_path`."""
    listing = "\n".join(f"- {t['path']}  ({t['label'][:70]})" for t in targets[:25])
    prompt = f"""You are an SEO specialist adding CONTEXTUAL INTERNAL LINKS inside a page's existing copy.

Business: {site_name}
Page: {page_title}

Other pages on this site you may link to (path — what the page is about):
{listing}

The page's visible copy:
\"\"\"{visible_text[:6000]}\"\"\"

Pick up to {MAX_LINKS_PER_PAGE} link opportunities. Rules:
- `phrase` must be an EXACT substring of the copy above (3-8 words), reading naturally as a link to that target — ideally containing the target's topic words.
- Prefer linking from body sentences, not headings or button labels.
- Each target at most once; skip anything that doesn't fit naturally. Fewer good links beat forced ones.

Respond with ONLY a JSON object: {{"links": [{{"phrase": "...", "target_path": "/x"}}]}} (empty list if nothing fits)."""
    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=800,
        system="You add internal links. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json(next((b.text for b in response.content if b.type == "text"), ""))
    return [l for l in (data.get("links") or [])
            if l.get("phrase") and str(l.get("target_path", "")).startswith("/")][:MAX_LINKS_PER_PAGE]


def _forbidden_spans(html: str) -> list:
    """Character spans where we must NOT inject a link (existing anchors,
    headings, scripts/styles, tag internals are excluded via tag-text matching)."""
    spans = []
    for m in re.finditer(r"<(a|h1|h2|h3|script|style|title|button|nav)\b.*?</\1>", html, re.I | re.S):
        spans.append((m.start(), m.end()))
    return spans


def _inject_links(html: str, links: list) -> tuple[str, list]:
    """Wrap each phrase's first safe occurrence in an <a>. Returns (html, applied)."""
    applied = []
    for l in links:
        phrase, target = l["phrase"].strip(), l["target_path"].rstrip("/") or "/"
        if not phrase or len(phrase) < 8:
            continue
        if re.search(r'href=["\']' + re.escape(target) + r'/?["\']', html, re.I):
            continue  # body already links this target somewhere
        spans = _forbidden_spans(html)
        for m in re.finditer(re.escape(phrase), html):
            i, j = m.start(), m.end()
            if any(a <= i < b for a, b in spans):
                continue
            # must be in text, not inside a tag: last '<' before i must be closed
            lt, gt = html.rfind("<", 0, i), html.rfind(">", 0, i)
            if lt > gt:
                continue
            html = html[:i] + f'<a href="{target}">{phrase}</a>' + html[j:]
            applied.append({"phrase": phrase, "target": target})
            break
    return html, applied


def run_context_links(site_id: int, run_id: int, conn: dict, page_id: int,
                      page_url: str = "", page_title: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        old_html = read_body(client, page_id)
        if old_html is None:
            run.status = "failed"
            run.summary = "Couldn't read the page body — is SEO Agent Bridge (v4+) active?"
            db.commit()
            return
        if not old_html:
            run.status = "completed"
            run.summary = "Page body is empty — nothing to link."
            db.commit()
            return

        # Link candidates: the site's mapped pages (label = target query), minus self.
        from urllib.parse import urlparse
        self_path = (urlparse(page_url).path or "").rstrip("/")
        targets = [{"path": t.page_path, "label": t.primary_kw}
                   for t in db.query(KeywordTarget).filter(KeywordTarget.site_id == site_id).all()
                   if t.page_path.rstrip("/") != self_path]
        if not targets:
            run.status = "completed"
            run.summary = "No keyword map yet — build it first (the Strategy panel), then link."
            db.commit()
            return

        soup = BeautifulSoup(old_html, "html.parser")
        for t in soup(["style", "script", "noscript"]):
            t.decompose()
        visible = soup.get_text(" ", strip=True)
        try:
            links = pick_link_spots(site.name, page_title or str(page_id), visible, targets)
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Link selection failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return
        if not links:
            run.status = "completed"
            run.summary = "No natural in-body link opportunities on this page."
            db.commit()
            return

        new_html, applied = _inject_links(old_html, links)
        if not applied or new_html == old_html:
            run.status = "completed"
            run.summary = "Suggested phrases weren't safely present in the copy — nothing changed."
            db.commit()
            return

        ok = write_body(client, page_id, new_html)
        db.add(SiteChange(
            site_id=site_id, kind="context_links",
            request=f"Add {len(applied)} contextual link(s) on {page_title or page_id}",
            css=new_html, old_css=old_html, status="applied" if ok else "failed",
            target_page_id=page_id, target_widget_id=""))
        if ok:
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "low_internal_links",
                    Finding.status.in_(("open", "in-progress")),
                    Finding.evidence_url.like(f"%{self_path or '/'}")).all():
                f.status = "closed"
                f.remark = f"Auto-fixed: {len(applied)} contextual internal link(s) added (live)."
            db.add(FixRecord(
                site_id=site_id, doer="Linking Agent", field="low_internal_links",
                action_taken=("Added contextual links on " + (page_title or str(page_id)) + ": "
                              + "; ".join(f'"{a["phrase"][:40]}" → {a["target"]}' for a in applied)),
                page_ref=str(page_id), before_value="(few in-body internal links)",
                after_value=f"{len(applied)} contextual links", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
        run.status = "completed"
        run.summary = (f"Added {len(applied)} contextual internal link(s) on {page_title or page_id} — live."
                       if ok else "Context-link write didn't verify.")
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


def start_context_links_async(site_id: int, run_id: int, conn: dict, page_id: int,
                              page_url: str = "", page_title: str = "") -> None:
    threading.Thread(target=run_context_links,
                     args=(site_id, run_id, conn, page_id, page_url, page_title), daemon=True).start()
