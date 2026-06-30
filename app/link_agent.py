"""Internal-linking doer — links orphaned pages into the site footer.

Ascend's auditor finds required pages (privacy/terms/about/contact/accessibility)
by *crawling internal links*: a page only counts as "present" if a discovered
link to it returns 200 (see crawler.py). So a page that's published but linked
nowhere is invisible to the audit — and to Google and to visitors. Publishing it
isn't enough; it has to be reachable.

This doer takes the required-page findings whose page already EXISTS but is
orphaned, and adds a link to it in the footer (beside the existing footer links
when possible, otherwise in a small block before </footer>). It writes through
the SEO Agent Bridge `_meridian_body` channel — the field the child theme prints
on the front end — then INDEPENDENTLY verifies by re-fetching the live homepage
and confirming the link is present and the target resolves 200. A finding is only
closed when that check passes. Idempotent: a link already present is left alone.
Pages that don't exist yet are NOT this doer's job — the required-page creator
(dispatcher._propose_required_page) publishes those first.
"""
import re
import threading
import time

import httpx

from .abilities import AbilitiesClient, USER_AGENT
from .database import SessionLocal
from .elementor_agent import list_elementor_pages, read_body, write_body
from .models import Approval, Finding, FixRecord, JobRun, RunLog, Site
from .website_agent import _page_type
from .wordpress import WordPressClient

# Conventional slug -> human label for the footer link (matches the slugs the
# required-page creator publishes at).
LINK_LABELS = {
    "privacy": "Privacy",
    "terms": "Terms",
    "about": "About",
    "contact": "Contact",
    "accessibility": "Accessibility",
}
KNOWN_TYPES = "|".join(LINK_LABELS)
# An existing footer link to one of the required pages — used as the anchor we
# insert the new link beside, so it inherits the footer's styling/context.
_REF_ANCHOR = re.compile(
    r'<a\b[^>]*href=["\']/(?:%s)/?["\'][^>]*>.*?</a>' % KNOWN_TYPES, re.I | re.S)


def _has_link(body: str, href: str) -> bool:
    """True if the body already links this href (slash-insensitive)."""
    h = href.rstrip("/")
    return bool(re.search(r'href=["\']%s/?["\']' % re.escape(h), body, re.I))


def _inject_footer_links(body: str, links: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Add an <a> for each (href, label) not already present, *inside the footer*
    (never the header/nav). Insert beside an existing footer required-page link
    when one exists (matching <li> vs inline context), else in a small <nav>
    before </footer>/</body>. Returns (new_body, added_hrefs)."""
    to_add = [(h, l) for (h, l) in links if not _has_link(body, h)]
    if not to_add:
        return body, []

    # Scope to the footer region so we never touch the header nav (which may also
    # link to /contact, /about, etc.). Use the LAST <footer> on the page.
    low = body.lower()
    fstart = low.rfind("<footer")
    fend = low.find("</footer>", fstart) if fstart != -1 else -1

    if fstart != -1 and fend != -1:
        m = _REF_ANCHOR.search(body, fstart, fend)
        if m:
            # Wrapped in a list item? Add <li> siblings; else inline with "·".
            prefix = body[max(fstart, m.start() - 16):m.start()]
            if re.search(r'<li[^>]*>\s*$', prefix, re.I):
                li_end = body.find("</li>", m.end())
                if li_end != -1 and li_end < fend:
                    insert = "".join(f'<li><a href="{h}">{l}</a></li>' for h, l in to_add)
                    return body[:li_end + 5] + insert + body[li_end + 5:], [h for h, _ in to_add]
            insert = "".join(f' · <a href="{h}">{l}</a>' for h, l in to_add)
            return body[:m.end()] + insert + body[m.end():], [h for h, _ in to_add]
        # Footer with no required-page link yet: drop a tidy block before </footer>.
        block = _block(to_add)
        return body[:fend] + block + body[fend:], [h for h, _ in to_add]

    # No <footer>: a self-contained block before </body>, else append.
    block = _block(to_add)
    idx = low.rfind("</body>")
    if idx != -1:
        return body[:idx] + block + body[idx:], [h for h, _ in to_add]
    return body + block, [h for h, _ in to_add]


def _block(links: list[tuple[str, str]]) -> str:
    return ('<nav class="seo-required-links" style="text-align:center;padding:10px;'
            'font-size:13px;opacity:.85">'
            + " · ".join(f'<a href="{h}">{l}</a>' for h, l in links) + '</nav>')


def _fetch(url: str) -> str:
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            return c.get(url).text
    except Exception:
        return ""


def _url_ok(base: str, href: str) -> bool:
    base = base.rstrip("/")
    url = base + "/" + href.lstrip("/")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            return c.get(url).status_code == 200
    except Exception:
        return False


def run_footer_links(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])

        findings = (db.query(Finding)
                    .filter(Finding.site_id == site_id,
                            Finding.category == "required_page_missing",
                            Finding.status.in_(("open", "in-progress")))
                    .all())
        if not findings:
            run.status = "completed"
            run.summary = "No required-page findings to link."
            db.commit()
            return

        # Which flagged pages actually EXIST (so they just need linking, not
        # creating)? Check the published slugs and, to be safe, the live URL.
        try:
            slugs = wp.page_slugs()
        except Exception:
            slugs = set()
        types = sorted({_page_type(f.issue) for f in findings})
        specs: list[tuple[str, str, str]] = []  # (page_type, href, label)
        for t in types:
            if t not in LINK_LABELS:
                continue
            href = f"/{t}"
            if t in slugs or _url_ok(site.url, href):
                specs.append((t, href, LINK_LABELS[t]))
        if not specs:
            run.status = "completed"
            run.summary = ("No orphaned pages to link — the flagged pages don't exist yet "
                           "(the required-page creator publishes those first).")
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        # Add the links to every page that has a footer (keeps the footer
        # consistent site-wide; the crawler only needs one, visitors want all).
        link_pairs = [(href, label) for _, href, label in specs]
        edited = 0
        for p in list_elementor_pages(conn):
            pid = p.get("id")
            if not pid:
                continue
            body = read_body(client, pid)
            if not body:
                continue
            new_body, added = _inject_footer_links(body, link_pairs)
            if added and new_body != body and write_body(client, pid, new_body):
                edited += 1

        # Independently verify: the link must be discoverable on the live homepage
        # (where the crawler starts) AND the target must resolve 200. Retry once
        # for the edge cache to catch up.
        verified: set[str] = set()
        for attempt in range(2):
            home = _fetch(site.url)
            verified = {t for (t, href, _) in specs if _has_link(home, href) and _url_ok(site.url, href)}
            if len(verified) == len(specs) or attempt == 1:
                break
            time.sleep(4)

        closed = 0
        for f in findings:
            t = _page_type(f.issue)
            if t in verified:
                f.status = "closed"
                f.remark = f"Linked the {t} page into the footer (verified live)."
                db.add(FixRecord(
                    site_id=site_id, finding_id=f.id, doer="Internal Linking",
                    field="required_page_missing",
                    action_taken=f"Added a footer link to the {t} page (/{t}) across {edited} page(s)",
                    page_ref=f"/{t}", before_value="(orphaned — published but linked nowhere)",
                    after_value=f"linked in footer (verified live on {site.url})",
                    method="auto-safe", lane="autonomous", applied=True,
                    verification_verdict="verified", status="done"))
                closed += 1

        # Clear now-redundant "Create <type> page" approvals for pages that exist
        # and are linked (they were proposing to create a page that's already live).
        for a in (db.query(Approval)
                  .filter(Approval.site_id == site_id, Approval.kind == "required_page",
                          Approval.status == "pending").all()):
            import json
            try:
                pt = (json.loads(a.payload or "{}") or {}).get("page_type")
            except Exception:
                pt = None
            if pt in verified:
                a.status = "rejected"
                a.summary = (a.summary or "") + "  ✓ Superseded: this page already exists and is now linked in the footer."

        run.status = "completed"
        if verified:
            run.summary = (f"Linked {len(verified)} orphaned page(s) into the footer "
                           f"across {edited} page(s) and closed {closed} finding(s) — verified live.")
        else:
            run.summary = ("Tried to link the orphaned page(s) but couldn't verify them live yet "
                           "(cache?) — will retry next run.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Internal-linking run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_footer_links_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_footer_links, args=(site_id, run_id, conn), daemon=True).start()
