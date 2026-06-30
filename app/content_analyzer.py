"""Content / E-E-A-T / GEO analyzer — Claude reads representative pages.

The crawler only measures structure. This fetches a few representative pages
(homepage + a couple of content/service pages) and has Claude judge content
quality, E-E-A-T, and AI-search citability, returning issue dicts that feed the
audit score. Best-effort: any failure (no API key, network) yields no issues and
never breaks the audit.
"""
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .brain import analyze_page_content

REQUEST_TIMEOUT = 12.0
USER_AGENT = "SEO-Agent-Auditor/1.0 (+read-only audit)"
MAX_PAGES = 15  # homepage + service/location/content pages — full-site content audit
                # (so the dispatcher can drive rewrites across every weak page)
UTILITY = ("contact", "privacy", "terms", "cart", "checkout", "login", "account",
           "thank", "search", "404", "wp-")


def _fetch(client: httpx.Client, url: str):
    try:
        r = client.get(url)
    except Exception:
        return None
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    title = (soup.title.get_text() if soup.title else "").strip()
    text = soup.get_text(" ", strip=True)
    return title, text, soup


def _pick_pages(client, start_url):
    """Homepage + up to 2 internal content/service pages discovered from it."""
    home = _fetch(client, start_url)
    if not home:
        return []
    pages = [start_url]
    _t, _x, soup = home
    seen = {urlparse(start_url).path.rstrip("/")}
    for a in soup.find_all("a", href=True):
        link = urljoin(start_url, a["href"]).split("#")[0]
        p = urlparse(link)
        if p.netloc != urlparse(start_url).netloc:
            continue
        path = p.path.rstrip("/")
        if not path or path in seen or any(u in path.lower() for u in UTILITY):
            continue
        seen.add(path)
        pages.append(link)
        if len(pages) >= MAX_PAGES:
            break
    return pages


def analyze_site_content(start_url: str, site_name: str) -> tuple[list[dict], int]:
    """Return (issues, pages_examined). pages_examined is the count of real content
    pages actually judged — the scorer divides content/GEO penalty by it so the
    score reflects average per-page quality, not how many pages we happened to read."""
    issues: list[dict] = []
    examined = 0
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT, headers=headers) as client:
        for url in _pick_pages(client, start_url):
            res = _fetch(client, url)
            if not res:
                continue
            title, text, _soup = res
            if len(text.split()) < 50:  # nothing to judge
                continue
            examined += 1
            try:
                analysis = analyze_page_content(site_name, url, title, text)
            except Exception:
                continue
            for f in analysis.get("findings", []):
                issues.append({
                    "category": f["category"], "severity": f["severity"],
                    "url": url, "detail": f["detail"], "detection_source": "ai",
                })
    return issues, examined
