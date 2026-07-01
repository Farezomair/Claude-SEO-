"""Redirects doer — 301 dead internal URLs to the best live page.

`broken_link` (internal) and `broken_page` findings mean an internal URL is dead
(404 / unreachable). Instead of hunting down every page that links to it, this
creates a 301 redirect from the dead path to the most topically relevant live page
(chosen by Claude) via the SEO Agent Bridge redirect map, then re-fetches each dead
URL to confirm it now resolves (200) before closing the finding.

Scope + safety: only INTERNAL dead URLs (we can't redirect other people's domains,
so external dead links are left for review). Required-page slugs (privacy/terms/…)
are skipped — those are the required-pages doer's job (creating the real page), and
a redirect would shadow the page it publishes. Fully reversible (clear the map).
Needs SEO Agent Bridge v7+ (the /redirects endpoint).
"""
import re
import threading
from urllib.parse import urlparse

import httpx

from .abilities import USER_AGENT
from .brain import pick_redirect_targets
from .database import SessionLocal
from .models import Finding, FixRecord, JobRun, RunLog, Site
from .wordpress import WordPressClient

# Conventional required-page slugs — owned by the required-pages doer, not this one.
_REQUIRED_SLUGS = ("privacy", "terms", "tos", "about", "contact", "accessibility")


def _base(conn: dict) -> str:
    u = conn["url"]
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _same_host(url: str, site_url: str) -> bool:
    try:
        a = urlparse(url).netloc.lower().removeprefix("www.")
        b = urlparse(site_url).netloc.lower().removeprefix("www.")
        return bool(a) and a == b
    except Exception:
        return False


def _dead_url(f) -> str:
    """The dead URL a finding is about: broken_page -> its evidence_url; broken_link
    -> the target parsed from the detail (the trailing http URL)."""
    if f.category == "broken_page":
        return f.evidence_url or ""
    m = re.search(r"(https?://\S+)$", (f.issue or "").strip())
    return m.group(1) if m else ""


def _norm_path(url: str) -> str:
    p = (urlparse(url).path or "/").strip("/")
    return "/" + p if p else "/"


def _redirects_post(conn: dict, payload: dict) -> bool:
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/redirects", json=payload)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _resolves(site_url: str, path: str) -> bool:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(site_url.rstrip("/") + path)
        return r.status_code == 200
    except Exception:
        return False


def run_redirects(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])

        findings = (db.query(Finding)
                    .filter(Finding.site_id == site_id,
                            Finding.category.in_(("broken_link", "broken_page")),
                            Finding.status.in_(("open", "in-progress"))).all())
        # Map each internal dead path -> the findings that reference it.
        dead: dict[str, list] = {}
        for f in findings:
            u = _dead_url(f)
            if not u or not _same_host(u, site.url):
                continue  # external / unknown -> can't redirect it
            path = _norm_path(u)
            if path == "/" or any(s in path.strip("/").split("/")[0] for s in _REQUIRED_SLUGS):
                continue  # homepage / required-page slug -> not ours
            dead.setdefault(path, []).append(f)
        if not dead:
            run.status = "completed"
            run.summary = "No internal dead URLs to redirect (external links can't be redirected)."
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        need = [p for p in dead if not _resolves(site.url, p)]   # still dead
        already = [p for p in dead if p not in need]             # resolve already

        setmap = {}
        if need:
            live_pages = []
            try:
                for it in wp.list_content(limit=100):
                    if it.get("link"):
                        live_pages.append({"path": _norm_path(it["link"]), "title": it.get("title", "")})
            except Exception:
                pass
            try:
                targets = pick_redirect_targets(site.name, live_pages, need)
            except Exception as exc:
                run.status = "failed"
                run.summary = f"Redirect-target selection failed: {exc.__class__.__name__}: {exc}"
                db.commit()
                return
            setmap = {p: t for p, t in targets.items() if t and t != p}
            if setmap and not _redirects_post(conn, {"set": setmap}):
                run.status = "failed"
                run.summary = "Couldn't write redirects — is SEO Agent Bridge (v7+) active?"
                db.add(RunLog(site_id=site_id, message=run.summary))
                db.commit()
                return

        # Verify: which dead paths now resolve (via the new 301) + the already-live ones.
        resolved = {p for p in setmap if _resolves(site.url, p)} | set(already)
        closed = 0
        for path, fs in dead.items():
            if path in resolved:
                for f in fs:
                    f.status = "closed"
                    tgt = setmap.get(path)
                    f.remark = (f"Auto-fixed: 301 redirect {path} → {tgt} (verified live)." if tgt
                                else f"Resolved — {path} now loads (verified live).")
                    closed += 1
        if setmap:
            db.add(FixRecord(
                site_id=site_id, doer="Redirects Agent", field="broken_link",
                action_taken=f"Added {len(setmap)} 301 redirect(s); {len(resolved)} dead URL(s) now resolve",
                page_ref=site.url, before_value=", ".join(list(setmap)[:6]),
                after_value=", ".join(f"{k} -> {v}" for k, v in list(setmap.items())[:6]),
                method="auto-safe", lane="autonomous", applied=True,
                verification_verdict="verified" if resolved else "not_fixed", status="done"))

        run.status = "completed"
        if resolved:
            run.summary = (f"Redirected {len(setmap)} dead URL(s); {len(resolved)} now resolve, "
                           f"closed {closed} finding(s) — verified live.")
        elif setmap:
            run.summary = "Wrote redirects but couldn't verify them live yet (cache?) — will retry next run."
        else:
            run.summary = "No suitable redirect targets found for the dead URLs."
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Redirects run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_redirects_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_redirects, args=(site_id, run_id, conn), daemon=True).start()
