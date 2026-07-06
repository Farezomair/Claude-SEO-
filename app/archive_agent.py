"""Archive-noindex doer — stop thin tag/category/author archives from diluting
the crawl.

`junk_archives` means auto-generated archive pages (default "Archives" titles,
no meta descriptions) are declared indexable in the sitemap — against a small
site's real pages that's a third of the crawlable site wasted. The honest fix is
Yoast's own switch: noindex those archive types (Yoast then also drops them from
its sitemaps automatically). This doer flips the switches through the Bridge,
then verifies on the live site — an archive page must actually serve a
noindex robots meta before the finding closes. Reversible (toggle back).
Needs SEO Agent Bridge v9+.
"""
import re
import threading
from urllib.parse import urlparse

import httpx

from .abilities import USER_AGENT
from .database import SessionLocal
from .models import Finding, FixRecord, JobRun, RunLog, Site


def _base(conn: dict) -> str:
    u = conn["url"]
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _archives_post(conn: dict, payload: dict):
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/yoast-archives", json=payload)
        return r.status_code, (r.json() if r.status_code < 500 else {})
    except Exception:
        return 0, {}


def _find_archive_url(site_url: str) -> str:
    """An archive URL to verify against — from the sitemap, else a common path."""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            for path in ("/sitemap_index.xml", "/sitemap.xml"):
                r = c.get(site_url.rstrip("/") + path)
                if r.status_code != 200:
                    continue
                locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.I | re.S)
                for sm in locs:
                    if re.search(r"(post_tag|category|author)-sitemap", sm):
                        rr = c.get(sm)
                        sublocs = re.findall(r"<loc>\s*(.*?)\s*</loc>", rr.text, re.I | re.S)
                        for u in sublocs:
                            if re.search(r"/(tag|category|author)/", u):
                                return u
                for u in locs:
                    if re.search(r"/(tag|category|author)/", u):
                        return u
    except Exception:
        pass
    return ""


def _is_noindexed(url: str) -> bool:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            html = c.get(url).text
        m = re.search(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
        return bool(m and "noindex" in m.group(1).lower())
    except Exception:
        return False


def run_archive_noindex(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        status, state = _archives_post(conn, {"tags": True, "categories": True, "authors": True})
        if status == 422:
            run.status = "completed"
            run.summary = "Yoast SEO isn't active on this site — archive noindexing needs it."
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return
        if status not in (200, 201):
            run.status = "failed"
            run.summary = "Couldn't set archive noindexing — is SEO Agent Bridge (v9+) active?"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        sample = _find_archive_url(site.url)
        verified = bool(sample) and _is_noindexed(sample)
        if verified:
            n = 0
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "junk_archives",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = "Auto-fixed: tag/category/author archives are now noindexed (verified live) — Yoast drops them from the sitemap automatically."
                n += 1
            db.add(FixRecord(
                site_id=site_id, doer="Technical Agent", field="junk_archives",
                action_taken="Noindexed tag, category, and author archives via Yoast (Bridge v9)",
                page_ref=sample or site.url, before_value="(archives indexable + in sitemap)",
                after_value="archives noindexed; excluded from sitemap", method="auto-safe",
                lane="autonomous", applied=True, verification_verdict="verified", status="done"))
            run.summary = f"Archives noindexed and verified live ({urlparse(sample).path}) — closed {n} finding(s)."
        else:
            run.summary = ("Archive noindex switches set" + (f", but {sample or 'no archive URL'} "
                           "didn't verify as noindexed yet (cache?) — will re-check next audit."))
        run.status = "completed"
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Archive-noindex run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_archive_noindex_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_archive_noindex, args=(site_id, run_id, conn), daemon=True).start()
