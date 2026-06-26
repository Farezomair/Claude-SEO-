"""Read-only website auditor (Stage 2).

Crawls a site starting from its homepage and reports problems. It NEVER writes
anything to the target site — only HTTP GET/HEAD requests. Guardrails from the
master plan are enforced here as hard, code-level caps so a crawl can never run
away: a max page count, a max number of link checks, and a per-request timeout.

Checks performed:
- broken_page : a crawled page returns HTTP >= 400 or fails to load
- broken_link : a link (internal or external) returns HTTP >= 400 or is unreachable
- structure   : a page is missing a <header> or <footer> region
"""
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# Many themes (esp. WordPress page builders) use <div class="...header...">
# rather than the semantic <header> tag, so we detect header/footer regions by
# tag OR ARIA role OR a class/id naming convention, to avoid false positives.
HEADER_RE = re.compile(r"header|masthead|navbar|topbar", re.I)
FOOTER_RE = re.compile(r"footer|colophon", re.I)

# Social "share" widgets are not navigational links and routinely block bots,
# so checking them produces false "broken link" noise. Skip them.
SHARE_RE = re.compile(
    r"(twitter\.com|x\.com)/intent|facebook\.com/sharer|"
    r"linkedin\.com/share|pinterest\.com/pin/create|"
    r"reddit\.com/submit|api\.whatsapp\.com|wa\.me",
    re.I,
)

# --- Hard caps (guardrails) ------------------------------------------------
MAX_PAGES = 30          # most pages we will crawl in one run
MAX_LINK_CHECKS = 150   # most distinct links we will status-check in one run
MAX_QUEUE = MAX_PAGES * 4
REQUEST_TIMEOUT = 8.0   # seconds per request
USER_AGENT = "SEO-Agent-Auditor/1.0 (+read-only audit)"


def _issue(category: str, severity: str, url: str, detail: str) -> dict:
    return {"category": category, "severity": severity, "url": url, "detail": detail}


def _normalize(url: str) -> str:
    """Drop the fragment so #anchors don't create duplicate URLs."""
    return urlparse(url)._replace(fragment="").geturl()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_domain(a: str, b: str) -> bool:
    return _domain(a) == _domain(b)


def _has_header(soup: BeautifulSoup) -> bool:
    return bool(
        soup.find("header")
        or soup.find(attrs={"role": "banner"})
        or soup.find(class_=HEADER_RE)
        or soup.find(id=HEADER_RE)
        or soup.find("nav")
    )


def _has_footer(soup: BeautifulSoup) -> bool:
    return bool(
        soup.find("footer")
        or soup.find(attrs={"role": "contentinfo"})
        or soup.find(class_=FOOTER_RE)
        or soup.find(id=FOOTER_RE)
    )


def crawl_site(start_url: str) -> dict:
    """Crawl ``start_url`` read-only and return {"issues": [...], "stats": {...}}."""
    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    issues: list[dict] = []
    visited: set[str] = set()
    queue: list[str] = [_normalize(start_url)]
    checked_links: dict[str, int | None] = {}   # link -> status code (or None if unreachable)
    reported_broken: set[str] = set()            # so one bad link isn't reported many times
    pages_crawled = 0
    links_checked = 0

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

            soup = BeautifulSoup(resp.text, "html.parser")

            # --- structure checks ---
            if not _has_header(soup):
                issues.append(_issue("structure", "medium", page_url, "No header region found on page"))
            if not _has_footer(soup):
                issues.append(_issue("structure", "medium", page_url, "No footer region found on page"))

            # --- indexation check (SEO backend) ---
            robots_meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
            if robots_meta and "noindex" in (robots_meta.get("content", "") or "").lower():
                issues.append(_issue("indexation", "medium", page_url,
                                     "Page is set to noindex — it won't appear in Google"))

            # --- link discovery + checks ---
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                link = _normalize(urljoin(page_url, href))
                if not link.startswith(("http://", "https://")):
                    continue
                if SHARE_RE.search(link):
                    continue  # share widget, not a real link

                internal = _same_domain(link, start_url)

                # queue internal pages for crawling
                if internal:
                    if (link not in visited and link not in queue
                            and len(visited) + len(queue) < MAX_QUEUE):
                        queue.append(link)

                # status-check the link (capped, deduplicated)
                if link not in checked_links and links_checked < MAX_LINK_CHECKS:
                    links_checked += 1
                    status = None
                    try:
                        r = client.head(link)
                        if r.status_code >= 400:  # some servers reject HEAD; confirm with GET
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
                                                     f"External link could not be verified "
                                                     f"(may block automated checks): {link}"))
                        elif status >= 400:
                            reported_broken.add(link)
                            sev = "high" if internal else "medium"
                            issues.append(_issue("broken_link", sev, page_url,
                                                 f"Link returns HTTP {status}: {link}"))

        # --- site-wide SEO backend checks (robots.txt + sitemap) ---
        origin = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
        try:
            rr = client.get(origin + "/robots.txt")
            if rr.status_code >= 400:
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

    stats = {
        "pages_crawled": pages_crawled,
        "links_checked": links_checked,
        "issues_found": len(issues),
    }
    return {"issues": issues, "stats": stats}
