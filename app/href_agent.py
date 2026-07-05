"""Href-rewrite doer — point every internal link at its FINAL URL.

The audit's `internal_redirect_links` check catches sites where internal links
route through 301s (e.g. hrefs to /page that redirect to /page/, or /blog/slug
that redirect to /slug/) — every internal PageRank flow takes a detour and every
click costs an extra round-trip. This doer resolves each internal href's final
URL once (cached), rewrites the hrefs in every page body to point straight at
it, writes back through the body channel, and then re-measures the live homepage
before closing the finding. Byte-preserving (only href values change), idempotent,
snapshot-revertible.
"""
import re
import threading
from urllib.parse import urljoin, urlparse

import httpx

from .database import SessionLocal
from .elementor_agent import AbilitiesClient, list_elementor_pages, read_body, write_body
from .models import Finding, FixRecord, JobRun, RunLog, Site, SiteChange

HREF_RE = re.compile(r'href=(["\'])([^"\']+)\1', re.I)
MAX_RESOLVES = 120
_SKIP_PREFIX = ("mailto:", "tel:", "javascript:", "#", "data:")


def _is_internal(href: str, host: str) -> bool:
    if href.startswith(_SKIP_PREFIX):
        return False
    if href.startswith("/"):
        return True
    h = urlparse(href).netloc.lower().removeprefix("www.")
    return bool(h) and h == host


def _site_relative(url: str) -> str:
    p = urlparse(url)
    return (p.path or "/") + (("?" + p.query) if p.query else "")


def _resolve_map(site_url: str, hrefs: set) -> dict:
    """{href: final_site_relative_url} for hrefs that redirect to a same-host 200."""
    host = urlparse(site_url).netloc.lower().removeprefix("www.")
    out = {}
    with httpx.Client(timeout=12.0, follow_redirects=True,
                      headers={"User-Agent": "SEO-Agent/1.0"}) as c:
        for href in sorted(hrefs)[:MAX_RESOLVES]:
            try:
                r = c.head(urljoin(site_url, href))
                if r.status_code >= 400:
                    r = c.get(urljoin(site_url, href))
            except Exception:
                continue
            if not r.history or r.status_code != 200:
                continue  # no redirect, or broken — not ours to rewrite
            final = str(r.url)
            fh = urlparse(final).netloc.lower().removeprefix("www.")
            if fh != host:
                continue  # redirected off-site — leave it
            rel = _site_relative(final)
            if rel and rel != href:
                out[href] = rel
    return out


def _redirect_rate(site_url: str) -> tuple[int, int]:
    """(redirected, checked) for the live homepage's internal links."""
    host = urlparse(site_url).netloc.lower().removeprefix("www.")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            html = c.get(site_url).text
            hrefs = {h for _q, h in HREF_RE.findall(html) if _is_internal(h, host)}
            checked = redirected = 0
            for h in sorted(hrefs)[:20]:
                try:
                    r = c.head(urljoin(site_url, h))
                    checked += 1
                    if r.history:
                        redirected += 1
                except Exception:
                    continue
            return redirected, checked
    except Exception:
        return 0, 0


def run_href_rewrite(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        host = urlparse(site.url).netloc.lower().removeprefix("www.")

        pages = list_elementor_pages(conn)[:40]
        bodies = {}
        all_hrefs: set = set()
        for p in pages:
            pid = p.get("id")
            if not pid:
                continue
            body = read_body(client, pid)
            if body:
                bodies[pid] = (p.get("title", ""), body)
                for _q, h in HREF_RE.findall(body):
                    if _is_internal(h, host):
                        all_hrefs.add(h)
        if not bodies:
            run.status = "failed"
            run.summary = "Couldn't read any page bodies — is SEO Agent Bridge active?"
            db.commit()
            return

        mapping = _resolve_map(site.url, all_hrefs)
        if not mapping:
            run.status = "completed"
            run.summary = "Internal links already point at their final URLs — nothing to rewrite."
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        pages_changed = links_changed = 0
        for pid, (title, body) in bodies.items():
            new_body = body
            n = 0
            for old, new in mapping.items():
                for q in ('"', "'"):
                    needle = f"href={q}{old}{q}"
                    if needle in new_body:
                        n += new_body.count(needle)
                        new_body = new_body.replace(needle, f"href={q}{new}{q}")
            if n and new_body != body and write_body(client, pid, new_body):
                pages_changed += 1
                links_changed += n
                db.add(SiteChange(
                    site_id=site_id, kind="href_rewrite",
                    request=f"Point {n} internal link(s) at their final URLs on {title or pid}",
                    css=new_body, old_css=body, status="applied",
                    target_page_id=pid, target_widget_id=""))
        db.commit()

        # Independent verification: re-measure the live homepage's redirect rate.
        redirected, checked = _redirect_rate(site.url)
        fixed = checked >= 5 and redirected / checked < 0.2
        if fixed:
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "internal_redirect_links",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = (f"Auto-fixed: {links_changed} internal link(s) across {pages_changed} page(s) now "
                            f"point at their final URLs ({redirected}/{checked} still redirect — verified live).")
        if links_changed:
            db.add(FixRecord(
                site_id=site_id, doer="Redirects Agent", field="internal_redirect_links",
                action_taken=f"Rewrote {links_changed} internal href(s) across {pages_changed} page(s) to final URLs",
                page_ref=site.url, before_value=f"{len(mapping)} redirecting link target(s)",
                after_value=f"live homepage: {redirected}/{checked} links redirect",
                method="auto-safe", lane="autonomous", applied=True,
                verification_verdict="verified" if fixed else "applied", status="done"))
        run.status = "completed"
        run.summary = (f"Rewrote {links_changed} internal link(s) across {pages_changed} page(s) — "
                       f"live homepage now has {redirected}/{checked} redirecting links."
                       if links_changed else "No page bodies contained the redirecting hrefs.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Href rewrite failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_href_rewrite_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_href_rewrite, args=(site_id, run_id, conn), daemon=True).start()
