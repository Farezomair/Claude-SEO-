"""Read-only website auditor (Phase B — expanded battery).

Crawls a site from its homepage and reports problems. NEVER writes to the target
site (only HTTP GET/HEAD). Hard caps: max pages, max link checks, per-request
timeout.

Checks (all crawl-based, no external API):
- site integrity : broken pages, broken links, redirect issues
- indexation     : robots.txt, sitemap, noindex, missing canonical
- structure      : missing header/footer region
- on-page        : missing/duplicate title, missing/multiple H1, missing viewport,
                   missing favicon, images missing alt
- required pages : privacy / contact / about / terms / accessibility present
- security       : HTTPS enforced, mixed content, core security headers
"""
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

HEADER_RE = re.compile(r"header|masthead|navbar|topbar", re.I)
FOOTER_RE = re.compile(r"footer|colophon", re.I)
ICON_RE = re.compile(r"icon", re.I)

# Social share widgets: not navigational links, routinely block bots. Skip.
SHARE_RE = re.compile(
    r"(twitter\.com|x\.com)/intent|facebook\.com/sharer|"
    r"linkedin\.com/share|pinterest\.com/pin/create|"
    r"reddit\.com/submit|api\.whatsapp\.com|wa\.me",
    re.I,
)
# Domains that block automated checkers, so a 4xx from them is not a broken link.
BOT_BLOCK_RE = re.compile(
    r"yelp\.com|instagram\.com|linkedin\.com|facebook\.com|tiktok\.com|"
    r"twitter\.com|x\.com|nextdoor\.com|tripadvisor\.com",
    re.I,
)
BOT_BLOCK_CODES = {403, 405, 429, 999}

MIXED_CONTENT_RE = re.compile(r"<(?:img|script|iframe|link|source)\b[^>]+(?:src|href)=[\"']http://", re.I)

# Required pages: category -> keywords that appear in the page path
REQUIRED_PAGES = {
    "privacy": (["privacy"], "medium"),
    "contact": (["contact"], "medium"),
    "about": (["about"], "low"),
    "terms": (["terms", "tos"], "low"),
    "accessibility": (["accessibility"], "low"),
}
SECURITY_HEADERS = {
    "strict-transport-security": "HSTS",
    "content-security-policy": "Content-Security-Policy",
    "x-frame-options": "X-Frame-Options",
    "x-content-type-options": "X-Content-Type-Options",
}

MAX_PAGES = 30
MAX_LINK_CHECKS = 150
MAX_QUEUE = MAX_PAGES * 4
REQUEST_TIMEOUT = 8.0
USER_AGENT = "SEO-Agent-Auditor/1.0 (+read-only audit)"


def _issue(category: str, severity: str, url: str, detail: str) -> dict:
    return {"category": category, "severity": severity, "url": url, "detail": detail}


def _normalize(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_domain(a: str, b: str) -> bool:
    return _domain(a) == _domain(b)


def _has_header(soup):
    return bool(soup.find("header") or soup.find(attrs={"role": "banner"})
               or soup.find(class_=HEADER_RE) or soup.find(id=HEADER_RE) or soup.find("nav"))


def _has_footer(soup):
    return bool(soup.find("footer") or soup.find(attrs={"role": "contentinfo"})
               or soup.find(class_=FOOTER_RE) or soup.find(id=FOOTER_RE))


def _page_title(soup) -> str:
    t = soup.find("title")
    return (t.get_text() or "").strip() if t else ""


def crawl_site(start_url: str) -> dict:
    """Crawl ``start_url`` read-only. Returns {"issues": [...], "stats": {...}}."""
    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    issues: list[dict] = []
    visited: set[str] = set()
    queue: list[str] = [_normalize(start_url)]
    checked_links: dict[str, int | None] = {}
    reported_broken: set[str] = set()
    internal_paths: set[str] = set()
    titles: dict[str, str] = {}            # url -> title (for duplicate detection)
    pages_crawled = 0
    links_checked = 0
    homepage_html = ""
    homepage_headers: dict = {}

    origin = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT, headers=headers) as client:
        while queue and pages_crawled < MAX_PAGES:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)

            try:
                resp = client.get(page_url)
            except Exception as exc:
                issues.append(_issue("broken_page", "high", page_url,
                                     f"Could not load page ({exc.__class__.__name__})"))
                continue

            pages_crawled += 1
            if resp.status_code >= 400:
                issues.append(_issue("broken_page", "high", page_url,
                                     f"Page returned HTTP {resp.status_code}"))
                continue
            if "text/html" not in resp.headers.get("content-type", ""):
                continue

            # Redirect issue: a chain longer than one hop, or a temporary (302).
            if len(resp.history) >= 2:
                issues.append(_issue("redirect_issue", "low", page_url,
                                     f"Redirect chain of {len(resp.history)} hops"))
            elif resp.history and resp.history[0].status_code == 302:
                issues.append(_issue("redirect_issue", "low", page_url,
                                     "Temporary (302) redirect that should likely be a permanent 301"))

            soup = BeautifulSoup(resp.text, "html.parser")
            if not homepage_html:
                homepage_html, homepage_headers = resp.text, dict(resp.headers)

            # --- structure ---
            if not _has_header(soup):
                issues.append(_issue("structure", "medium", page_url, "No header region found on page"))
            if not _has_footer(soup):
                issues.append(_issue("structure", "medium", page_url, "No footer region found on page"))

            # --- indexation ---
            robots_meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
            if robots_meta and "noindex" in (robots_meta.get("content", "") or "").lower():
                issues.append(_issue("indexation", "medium", page_url,
                                     "Page is set to noindex — it won't appear in Google"))
            if not soup.find("link", attrs={"rel": re.compile(r"canonical", re.I)}):
                issues.append(_issue("missing_canonical", "low", page_url,
                                     "No canonical tag on page"))

            # --- on-page mechanics ---
            title = _page_title(soup)
            if not title:
                issues.append(_issue("missing_title", "high", page_url, "Page has no <title> tag"))
            else:
                titles[page_url] = title
            h1s = soup.find_all("h1")
            if len(h1s) == 0:
                issues.append(_issue("missing_h1", "medium", page_url, "Page has no H1 heading"))
            elif len(h1s) > 1:
                issues.append(_issue("multiple_h1", "low", page_url, f"Page has {len(h1s)} H1 headings (should be one)"))
            if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}):
                issues.append(_issue("missing_viewport", "medium", page_url,
                                     "No viewport meta — page may not be mobile-friendly"))
            imgs = soup.find_all("img")
            no_alt = [i for i in imgs if i.get("alt") is None]
            if no_alt:
                issues.append(_issue("images_missing_alt", "low", page_url,
                                     f"{len(no_alt)} of {len(imgs)} images missing alt text"))

            # --- security: mixed content on HTTPS pages ---
            if page_url.startswith("https://") and MIXED_CONTENT_RE.search(resp.text):
                issues.append(_issue("mixed_content", "medium", page_url,
                                     "Page loads some assets over insecure HTTP (mixed content)"))

            # --- link discovery + checks ---
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                link = _normalize(urljoin(page_url, href))
                if not link.startswith(("http://", "https://")):
                    continue
                if SHARE_RE.search(link):
                    continue
                internal = _same_domain(link, start_url)
                if internal:
                    internal_paths.add(urlparse(link).path.lower())
                    if (link not in visited and link not in queue
                            and len(visited) + len(queue) < MAX_QUEUE):
                        queue.append(link)

                if link not in checked_links and links_checked < MAX_LINK_CHECKS:
                    links_checked += 1
                    status = None
                    try:
                        r = client.head(link)
                        if r.status_code >= 400:
                            r = client.get(link)
                        status = r.status_code
                    except Exception:
                        status = None
                    checked_links[link] = status

                    if link not in reported_broken:
                        if status is None:
                            reported_broken.add(link)
                            if internal:
                                issues.append(_issue("broken_link", "high", page_url,
                                                     f"Internal link could not be reached: {link}"))
                            else:
                                issues.append(_issue("broken_link", "low", page_url,
                                                     f"External link could not be verified: {link}"))
                        elif status >= 400:
                            reported_broken.add(link)
                            # External 4xx from a known bot-blocker is not a broken link.
                            if not internal and (status in BOT_BLOCK_CODES or BOT_BLOCK_RE.search(link)):
                                issues.append(_issue("broken_link", "low", page_url,
                                                     f"External link returns HTTP {status} (likely blocks automated checks): {link}"))
                            else:
                                sev = "high" if internal else "medium"
                                issues.append(_issue("broken_link", sev, page_url,
                                                     f"Link returns HTTP {status}: {link}"))

        # ---------------- site-wide checks ----------------
        # robots.txt + sitemap
        try:
            if client.get(origin + "/robots.txt").status_code >= 400:
                issues.append(_issue("indexation", "low", origin, "No robots.txt found"))
        except Exception:
            pass
        sitemap_found = False
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"):
            try:
                sr = client.get(origin + path)
                if sr.status_code == 200 and "<" in sr.text[:200]:
                    sitemap_found = True
                    break
            except Exception:
                pass
        if not sitemap_found:
            issues.append(_issue("indexation", "medium", origin,
                                 "No sitemap.xml found — search engines may miss pages"))

        # required pages (via discovered internal links)
        for name, (keywords, sev) in REQUIRED_PAGES.items():
            if not any(any(k in p for k in keywords) for p in internal_paths):
                issues.append(_issue("required_page_missing", sev, origin,
                                     f"No {name} page found in the site's links"))

        # favicon
        if homepage_html and not BeautifulSoup(homepage_html, "html.parser").find(
                "link", attrs={"rel": ICON_RE}):
            try:
                if client.get(origin + "/favicon.ico").status_code >= 400:
                    issues.append(_issue("missing_favicon", "low", origin, "No favicon found"))
            except Exception:
                pass

        # security headers (homepage)
        if homepage_headers:
            lower = {k.lower(): v for k, v in homepage_headers.items()}
            missing = [label for h, label in SECURITY_HEADERS.items() if h not in lower]
            if missing:
                issues.append(_issue("security_headers", "low", origin,
                                     "Missing security headers: " + ", ".join(missing)))

        # HTTPS enforcement
        try:
            http_resp = client.get("http://" + urlparse(start_url).netloc)
            if str(http_resp.url).startswith("http://"):
                issues.append(_issue("no_https", "high", origin,
                                     "Site does not redirect HTTP to HTTPS"))
        except Exception:
            pass

        # duplicate titles
        seen: dict[str, list[str]] = {}
        for url, t in titles.items():
            seen.setdefault(t, []).append(url)
        for t, urls in seen.items():
            if len(urls) > 1:
                issues.append(_issue("duplicate_title", "medium", urls[0],
                                     f"Title \"{t[:60]}\" is duplicated on {len(urls)} pages"))

    stats = {"pages_crawled": pages_crawled, "links_checked": links_checked, "issues_found": len(issues)}
    return {"issues": issues, "stats": stats}
