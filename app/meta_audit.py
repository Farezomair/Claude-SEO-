"""Title-conflict audit — does the RENDERED page respect the intended title?

On theme-built sites two title sources can conflict: the SEO plugin (Yoast) holds
the intended title, but the theme's <head> wins on the live page — so the title
Google indexes can be a junk default like "Homepage | Site". Stored-meta
verification alone would never notice. This check compares each page's Yoast
title against the live rendered <title> and flags mismatches as
`title_conflict` (the Meta Agent also re-verifies against the rendered page after
every write, so it can't claim a fix the theme swallowed).
"""
import re

import httpx

from .connections import get_connection
from .wordpress import WordPressClient, YOAST_TITLE_KEY

MAX_CHECKS = 10


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def rendered_title(url: str) -> str:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "SEO-Agent-Auditor/1.0"}) as c:
            html = c.get(url).text
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        return _norm(m.group(1)) if m else ""
    except Exception:
        return ""


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
