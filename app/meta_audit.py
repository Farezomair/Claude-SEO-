"""Title-conflict audit — does the RENDERED page respect the intended title?

On theme-built sites two title sources can conflict: the SEO plugin (Yoast) holds
the intended title, but the theme's <head> wins on the live page — so the title
Google indexes can be a junk default like "Homepage | Site". Stored-meta
verification alone would never notice. This check compares each page's Yoast
title against the live rendered <title> and flags mismatches as
`title_conflict` (the Meta Agent also re-verifies against the rendered page after
every write, so it can't claim a fix the theme swallowed).
"""
import html as _html
import re

import httpx

from .connections import get_connection
from .wordpress import WordPressClient, YOAST_TITLE_KEY

MAX_CHECKS = 10


def _norm(t: str) -> str:
    # Unescape entities so "&amp;" == "&" — otherwise a rendered title that
    # matches its SEO title byte-for-byte still looks like a conflict.
    return re.sub(r"\s+", " ", _html.unescape(t or "")).strip().lower()


def rendered_title(url: str) -> str:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "SEO-Agent-Auditor/1.0"}) as c:
            html = c.get(url).text
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        return _norm(m.group(1)) if m else ""
    except Exception:
        return ""


def run_title_override(site_id: int, run_id: int, conn: dict) -> None:
    """Title-override doer (Bridge v9): make the RENDERED page serve the intended
    SEO title even when the theme hardcodes its own <title>. Enables the Bridge
    override (document-title filter + output-buffer rewrite), then re-fetches each
    flagged page and only closes `title_conflict` findings whose live title now
    matches the Yoast title. Reversible (toggle off)."""
    import threading as _t  # noqa: F401  (keeps import section tidy)
    from .database import SessionLocal
    from .models import Finding, FixRecord, JobRun, RunLog, Site
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        base = conn["url"].rstrip("/")
        if not base.startswith("http"):
            base = "https://" + base
        try:
            with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                              follow_redirects=True) as c:
                r = c.post(base + "/wp-json/seo-agent/v1/title-override", json={"enabled": True})
            ok = r.status_code in (200, 201) and (r.json() or {}).get("enabled")
        except Exception:
            ok = False
        if not ok:
            run.status = "failed"
            run.summary = "Couldn't enable the title override — is SEO Agent Bridge (v9+) active?"
            db.add(RunLog(site_id=site_id, message=run.summary))
            db.commit()
            return

        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
        yoast_by_link = {}
        try:
            for it in wp.list_content(limit=60):
                y = _norm((it.get("meta") or {}).get(YOAST_TITLE_KEY) or "")
                if y and it.get("link"):
                    yoast_by_link[it["link"].rstrip("/")] = y
        except Exception:
            pass

        closed = 0
        findings = (db.query(Finding)
                    .filter(Finding.site_id == site_id, Finding.category == "title_conflict",
                            Finding.status.in_(("open", "in-progress"))).all())
        for f in findings:
            url = (f.evidence_url or "").rstrip("/")
            yoast = yoast_by_link.get(url, "")
            live = rendered_title(f.evidence_url or "")
            if yoast and live and (yoast in live or live in yoast):
                f.status = "closed"
                f.remark = f"Auto-fixed: the rendered title now serves the SEO title (“{live[:60]}”) — verified live."
                closed += 1
        if closed:
            db.add(FixRecord(
                site_id=site_id, doer="Meta Agent", field="title_conflict",
                action_taken="Enabled the Bridge title override — the SEO title now wins over the theme's <title>",
                page_ref=site.url, before_value="(theme overrode the SEO title)",
                after_value=f"{closed} page(s) verified serving the intended title",
                method="auto-safe", lane="autonomous", applied=True,
                verification_verdict="verified", status="done"))
        run.status = "completed"
        run.summary = (f"Title override enabled — {closed} of {len(findings)} flagged page(s) verified fixed live."
                       if findings else "Title override enabled — no flagged pages to verify.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Title-override run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_title_override_async(site_id: int, run_id: int, conn: dict) -> None:
    import threading
    threading.Thread(target=run_title_override, args=(site_id, run_id, conn), daemon=True).start()


def title_conflict_findings(site_id: int, site_url: str, site_name: str = "") -> list:
    conn = get_connection(site_id, site_url, site_name)
    if not conn:
        return []
    wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
    issues = []
    checked = 0
    try:
        items = wp.list_content(limit=40)
    except Exception:
        return []
    for it in items:
        if checked >= MAX_CHECKS:
            break
        yoast = _norm((it.get("meta") or {}).get(YOAST_TITLE_KEY) or "")
        if not yoast or not it.get("link"):
            continue
        live = rendered_title(it["link"])
        if not live:
            continue
        checked += 1
        # The intended title should be the rendered title (allowing for an
        # appended brand suffix). If it's nowhere in the live <title>, the theme
        # is overriding the SEO plugin and Google indexes the wrong title.
        if yoast not in live and live not in yoast:
            issues.append({
                "category": "title_conflict", "severity": "medium", "url": it["link"],
                "detail": (f'Rendered title is "{live[:60]}" but the SEO title is set to '
                           f'"{yoast[:60]}" — the theme overrides the SEO plugin, so Google '
                           "indexes the wrong title"),
                "detection_source": "meta-audit",
            })
    return issues
