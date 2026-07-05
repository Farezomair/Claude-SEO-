"""The Keyword Brain — the strategy layer that makes the crew query-aware.

Rankings are a tournament for specific queries, so before optimizing anything
you must decide what each page should rank FOR. This module builds that target
keyword map — from the business profile plus real Search Console demand — and
serves it to the rest of the system:

- `run_keyword_map` (JobRun kind "keywords"): builds/refreshes the map.
- `keyword_for(db, site_id, url)`: the target for one page — read by the meta,
  ranking, and rewrite doers so every word they write aims at the target query.
- `targeting_findings(...)`: the "keyword_targeting" audit check — flags mapped
  pages whose title/H1 don't reflect their target query (auto-fixed by the
  query-aware Meta Agent).
"""
import json
import threading
from urllib.parse import urlparse

import httpx

from .brain import build_keyword_map
from .database import SessionLocal
from .gsc import queries_by_page
from .models import JobRun, KeywordTarget, RunLog, Site
from .wordpress import WordPressClient

MAX_TARGETING_CHECKS = 12
_SKIP_PATHS = ("privacy", "terms", "accessibility", "contact", "login", "cart",
               "checkout", "thank", "search", "404")


def _norm_path(url: str) -> str:
    p = (urlparse(url).path or "/").rstrip("/")
    return p or "/"


def keyword_for(db, site_id: int, url: str) -> str:
    """The primary target query mapped to this page ('' when unmapped)."""
    try:
        row = (db.query(KeywordTarget)
               .filter(KeywordTarget.site_id == site_id,
                       KeywordTarget.page_path == _norm_path(url)).first())
        return row.primary_kw if row else ""
    except Exception:
        return ""


def run_keyword_map(site_id: int, run_id: int, conn: dict) -> None:
    """Build (or rebuild) the site's target keyword map."""
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
        pages = []
        try:
            for it in wp.list_content(limit=60):
                if it.get("link"):
                    pages.append({"path": _norm_path(it["link"]), "title": it.get("title", "")})
        except Exception:
            pass
        if not pages:
            run.status = "failed"
            run.summary = "Couldn't list the site's pages to build the keyword map."
            db.commit()
            return
        gsc_pages = queries_by_page(site.url)  # {} when GSC isn't connected
        try:
            kw_map = build_keyword_map(site.name, site.url, pages, gsc_pages)
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Keyword mapping failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return
        if not kw_map:
            run.status = "completed"
            run.summary = "The strategist returned no keyword targets."
            db.commit()
            return

        # Fresh map is the source of truth: replace the old one.
        db.query(KeywordTarget).filter(KeywordTarget.site_id == site_id).delete()
        for m in kw_map:
            db.add(KeywordTarget(
                site_id=site_id, page_path=m["path"], primary_kw=m["primary"],
                secondary_kws=json.dumps(m["secondary"]), intent=m["intent"],
                rationale=m["rationale"],
                source="ai+gsc" if gsc_pages.get(m["path"]) else "ai"))
        run.status = "completed"
        run.summary = (f"Keyword map built: {len(kw_map)} page(s) targeted"
                       + (f", grounded in real Search Console demand for {sum(1 for m in kw_map if gsc_pages.get(m['path']))} of them."
                          if gsc_pages else " (connect Google to ground it in real search demand)."))
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Keyword Brain failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_keyword_map_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_keyword_map, args=(site_id, run_id, conn), daemon=True).start()


def ensure_keyword_map(site_id: int, conn: dict) -> None:
    """Build the map synchronously if none exists (called by the weekly run so
    a new site gets a strategy before its first fixes)."""
    db = SessionLocal()
    try:
        if db.query(KeywordTarget).filter(KeywordTarget.site_id == site_id).count():
            return
        run = JobRun(site_id=site_id, kind="keywords", status="running",
                     summary="Building the keyword map…")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()
    run_keyword_map(site_id, run_id, conn)


def targeting_findings(site_id: int, site_url: str) -> list:
    """Audit check `keyword_targeting`: a mapped page whose <title>/<h1> don't
    mention its target query (or a close part of it) isn't really competing for
    it. Emitted like any crawler check; fixed by the query-aware Meta Agent."""
    db = SessionLocal()
    try:
        targets = (db.query(KeywordTarget)
                   .filter(KeywordTarget.site_id == site_id).all())
    finally:
        db.close()
    issues = []
    if not targets:
        return issues
    base = site_url.rstrip("/")
    checked = 0
    with httpx.Client(timeout=15.0, follow_redirects=True,
                      headers={"User-Agent": "SEO-Agent-Auditor/1.0"}) as c:
        for t in targets:
            if checked >= MAX_TARGETING_CHECKS:
                break
            if any(s in t.page_path for s in _SKIP_PATHS):
                continue
            try:
                r = c.get(base + t.page_path)
                if r.status_code != 200:
                    continue
            except Exception:
                continue
            checked += 1
            html = r.text.lower()
            import re as _re
            title = (_re.search(r"<title[^>]*>(.*?)</title>", html, _re.S) or [None, ""])[1]
            h1 = (_re.search(r"<h1[^>]*>(.*?)</h1>", html, _re.S) or [None, ""])[1]
            head = f"{title} {h1}"
            kw = t.primary_kw.lower()
            words = [w for w in _re.findall(r"[a-z0-9]+", kw) if len(w) > 2]
            hit = sum(1 for w in words if w in head)
            if words and hit < max(1, len(words) - 1):  # allow one missing word
                issues.append({
                    "category": "keyword_targeting", "severity": "medium",
                    "url": base + t.page_path,
                    "detail": (f'Page is mapped to rank for "{t.primary_kw}" but its title/H1 '
                               "don't say so — Google can't rank it for a query it never mentions"),
                    "detection_source": "keyword-brain",
                })
    return issues
