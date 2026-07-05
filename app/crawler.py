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
import json
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

# Thin-content: skip short-by-nature utility pages; flag real content pages below
# the threshold.
THIN_THRESHOLD = 250
UTILITY_PATHS = ("contact", "privacy", "terms", "about", "cart", "checkout",
                 "login", "account", "thank", "search", "404")

# Schema validation (grounded in the seo-schema skill).
DEPRECATED_SCHEMA = {"howto", "faqpage"}  # HowTo removed; FAQ restricted to gov/health
PLACEHOLDER_MARKERS = ("lorem ipsum", "your business name", "example.com",
                       "placeholder", "{{", "xxxxx", "todo")


def _schema_types(data) -> set:
    """Collect every @type string anywhere in a JSON-LD blob (lowercased)."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                found.add(t.lower())
            elif isinstance(t, list):
                found.update(str(x).lower() for x in t)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found


AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "anthropic-ai",
               "PerplexityBot", "Google-Extended", "CCBot", "Bytespider"]
ENTITY_SCHEMA = {"organization", "localbusiness", "website", "person", "professionalservice"}
# Common LocalBusiness subtypes (lowercased). The schema generator emits the most
# SPECIFIC subtype (e.g. GeneralContractor), so detection must recognize them too —
# otherwise an injected GeneralContractor block isn't seen and the finding loops
# forever. Shared with schema_agent so the doer's guard and the detector agree.
LOCALBUSINESS_SUBTYPES = {
    "localbusiness", "organization", "professionalservice", "store", "shoppingcenter",
    "generalcontractor", "homeandconstructionbusiness", "plumber", "electrician",
    "roofingcontractor", "hvacbusiness", "locksmith", "movingcompany", "housepainter",
    "landscaper", "cleaningservice", "selfstorage", "pestcontrolservice",
    "restaurant", "foodestablishment", "cafeorcoffeeshop", "bakery", "bar", "barorpub",
    "dentist", "physician", "hospital", "medicalbusiness", "medicalclinic", "pharmacy",
    "veterinarycare", "healthandbeautybusiness", "beautysalon", "hairsalon", "daycare",
    "legalservice", "attorney", "lawfirm", "accountingservice", "financialservice",
    "insuranceagency", "realestateagent", "automotivebusiness", "autorepair", "autodealer",
    "gym", "sportsactivitylocation", "lodgingbusiness", "hotel", "travelagency",
    "emergencyservice", "childcare", "fooddeliveryservice", "foodservice",
}
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")


# Imgix-style image CDNs where `auto=format` enables WebP/AVIF content negotiation.
IMGIX_HOSTS = ("images.unsplash.com", "images.pexels.com")

# ---- Trust layer (what real auditors check: truth, not just syntax) ----
# 555-01XX is the fictional phone exchange reserved for film/TV — it cannot
# receive calls. A business showing one fails every verification check.
FAKE_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?555[\s.\-]?01\d{2}")
# Placeholder credentials presented as real (sequential/zero digits).
FAKE_LICENSE_RE = re.compile(
    r"(?:lic(?:ense|ence)?\.?|registration)\s*(?:no\.?|number|#)?\s*[:#]?\s*[A-Z]{0,5}-?(?:12345\d*|00000\d*|1234)\b",
    re.I)
STOCK_HOSTS = ("images.unsplash.com", "images.pexels.com", "cdn.pixabay.com",
               "img.freepik.com", "media.istockphoto.com")
STALE_YEAR_RE = re.compile(r"\b(201\d|202[0-5])\b")
CURRENT_YEAR = 2026
# Crawler-extracted heading tokens glued together by span markup ("KitchenExperts").
CONCAT_RE = re.compile(r"[a-z][A-Z][a-z]")


def _legacy_format_src(src: str) -> bool:
    """True if this image src serves a legacy format a doer could modernize:
    an imgix-style CDN URL missing auto=format, or a plain .jpg/.png file."""
    if not src or src.startswith("data:"):
        return False
    low = src.lower()
    if any(h in low for h in IMGIX_HOSTS):
        return "auto=format" not in low
    return bool(re.search(r"\.(jpe?g|png)(\?|#|$)", low)) and ".webp" not in low


def _ai_blocked(robots_txt: str) -> list[str]:
    """Return which AI crawlers are disallowed from the whole site in robots.txt."""
    blocked: set[str] = set()
    for block in re.split(r"\n\s*\n", robots_txt or ""):
        agents = [m.strip().lower() for m in re.findall(r"(?im)^user-agent:\s*(.+)$", block)]
        disallows = [m.strip() for m in re.findall(r"(?im)^disallow:\s*(.*)$", block)]
        if "/" in disallows:
            for bot in AI_CRAWLERS:
                if bot.lower() in agents:
                    blocked.add(bot)
    return sorted(blocked)


def _home_schema_types(home_soup) -> set:
    types: set[str] = set()
    if home_soup is None:
        return types
    for s in home_soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = s.string or s.get_text() or ""
        try:
            types |= _schema_types(json.loads(raw))
        except Exception:
            pass
    return types


def _sitemap_locs(client, text: str, depth: int = 0) -> set:
    """Collect <loc> URLs from a sitemap, following one level of sitemap index."""
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S)
    if "<sitemapindex" in text.lower() and depth < 1:
        urls: set[str] = set()
        for sm in locs[:6]:
            try:
                sub = client.get(sm)
                if sub.status_code == 200:
                    urls |= _sitemap_locs(client, sub.text, depth + 1)
            except Exception:
                pass
        return urls
    return set(locs)

MAX_PAGES = 30


def _max_crawl_pages() -> int:
    """Enhance-bar tunable: how many pages the crawler examines per audit."""
    from .capabilities import cap_setting
    try:
        return int(cap_setting("audit:technical", "crawl_pages", MAX_PAGES))
    except Exception:
        return MAX_PAGES


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

    home_norm = _normalize(start_url)
    issues: list[dict] = []
    visited: set[str] = set()
    queue: list[str] = [home_norm]
    checked_links: dict[str, int | None] = {}
    reported_broken: set[str] = set()
    schema_reported: set[str] = set()   # dedupe template-wide schema issues
    internal_paths: set[str] = set()
    ok_paths: set[str] = set()   # paths that actually returned 200 HTML (for required-page presence)
    heading_skip = {"count": 0, "url": "", "detail": ""}  # collapse template-wide heading skips into one finding
    linked_internal: set[str] = set()   # internal URLs seen as links (for orphan detection)
    titles: dict[str, str] = {}            # url -> title (for duplicate detection)
    pages_crawled = 0
    links_checked = 0
    homepage_html = ""
    homepage_headers: dict = {}

    origin = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT, headers=headers) as client:
        max_pages = _max_crawl_pages()
        trust_reported: set = set()
        int_links = {"checked": 0, "redirected": 0, "example": ""}
        while queue and pages_crawled < max_pages:
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
            ok_paths.add((urlparse(page_url).path.lower() or "/"))

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
            # Images without width/height — a Core Web Vitals (CLS) risk.
            no_dim = [i for i in imgs if not (i.get("width") and i.get("height"))]
            if imgs and len(no_dim) >= max(3, len(imgs) // 2):
                issues.append(_issue("image_no_dimensions", "low", page_url,
                                     f"{len(no_dim)} of {len(imgs)} images have no width/height set (can cause layout shift / CLS)"))
            # Contextual internal links — in-body links (outside header/footer/nav)
            # pass authority and context; a content page with almost none is a
            # dead end for both Google and visitors.
            path_l = (urlparse(page_url).path or "/").lower()
            if not any(u in path_l for u in UTILITY_PATHS):
                body_soup = BeautifulSoup(resp.text, "html.parser")
                for t in body_soup(["header", "footer", "nav", "script", "style"]):
                    t.decompose()
                host = urlparse(page_url).netloc.lower().removeprefix("www.")
                ctx_links = 0
                for a_tag in body_soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.startswith("#") or href.startswith(("mailto:", "tel:")):
                        continue
                    h = urlparse(href).netloc.lower().removeprefix("www.")
                    if (not h or h == host) and (urlparse(href).path or "/") not in ("/", path_l):
                        ctx_links += 1
                if ctx_links < 2:
                    issues.append(_issue("low_internal_links", "low", page_url,
                                         f"Only {ctx_links} contextual in-body link(s) to other pages — "
                                         "internal links pass authority and guide visitors"))

            # Legacy image formats — JPEG/PNG where WebP/AVIF could serve instead.
            # Two cases: an imgix-style CDN (Unsplash/Pexels) missing auto=format
            # (defaults to JPEG), or a plain .jpg/.png file.
            legacy = [i for i in imgs if _legacy_format_src(i.get("src") or "")]
            if legacy:
                issues.append(_issue("image_legacy_format", "low", page_url,
                                     f"{len(legacy)} of {len(imgs)} images serve legacy formats (JPEG/PNG) — "
                                     "WebP/AVIF would load faster"))

            # --- on-page depth (expanded battery) ---
            md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            if not md or not (md.get("content") or "").strip():
                issues.append(_issue("meta_description_missing", "medium", page_url,
                                     "No meta description — Google may show a random snippet"))
            if title:
                if len(title) > 65:
                    issues.append(_issue("title_length", "low", page_url,
                                         f"Title is long ({len(title)} chars) — likely truncated in search results"))
                elif len(title) < 25:
                    issues.append(_issue("title_length", "low", page_url,
                                         f"Title is short ({len(title)} chars) — wasted SERP space"))
                yr = STALE_YEAR_RE.search(title)
                if yr and int(yr.group(1)) < CURRENT_YEAR:
                    issues.append(_issue("stale_year_title", "low", page_url,
                                         f"Title still says {yr.group(1)} — stale-dated content loses clicks and trust"))
            # Heading hierarchy: flag a skipped level (e.g. H2 -> H4). This is
            # template-wide on builder sites, so count pages and report ONCE below.
            levels = [int(h.name[1]) for h in soup.find_all(re.compile(r"^h[1-6]$"))]
            prev = 0
            for lvl in levels:
                if prev and lvl > prev + 1:
                    heading_skip["count"] += 1
                    if not heading_skip["url"]:
                        heading_skip["url"] = page_url
                        heading_skip["detail"] = f"Heading levels skip from H{prev} to H{lvl}"
                    break
                prev = lvl
            if "heading_concat" not in trust_reported:
                for h in soup.find_all(re.compile(r"^h[1-3]$")):
                    txt = h.get_text("", strip=True)
                    if CONCAT_RE.search(txt):
                        trust_reported.add("heading_concat")
                        issues.append(_issue("heading_concat", "medium", page_url,
                                             f"Headings render with glued words (e.g. “{txt[:60]}”) — the theme's "
                                             "line-break markup drops the space, so crawlers can't parse the keywords"))
                        break

            # --- thin content (SEO Auditor group C) ---
            path = urlparse(page_url).path.lower()
            if not any(u in path for u in UTILITY_PATHS):
                word_count = len(soup.get_text(" ", strip=True).split())
                if word_count < THIN_THRESHOLD:
                    issues.append(_issue("thin_content", "medium", page_url,
                                         f"Thin content: about {word_count} words for this page"))

            # --- schema validity (SEO Auditor group F; template-wide dedup) ---
            lb_count = {"n": 0}
            ld_scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
            if not ld_scripts and page_url == home_norm:
                issues.append(_issue("missing_schema", "low", page_url,
                                     "No structured data (JSON-LD) on the homepage"))
            for s in ld_scripts:
                raw = s.string or s.get_text() or ""
                if not raw.strip():
                    continue
                if "schema_invalid" not in schema_reported:
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        schema_reported.add("schema_invalid")
                        issues.append(_issue("schema_invalid", "medium", page_url,
                                             "Structured data (JSON-LD) is not valid JSON"))
                        continue
                else:
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        continue
                if "schema_placeholder" not in schema_reported and any(
                        m in raw.lower() for m in PLACEHOLDER_MARKERS):
                    schema_reported.add("schema_placeholder")
                    issues.append(_issue("schema_placeholder", "medium", page_url,
                                         "Structured data contains placeholder/template text"))
                if "schema_deprecated" not in schema_reported:
                    bad = _schema_types(parsed) & DEPRECATED_SCHEMA
                    if bad:
                        schema_reported.add("schema_deprecated")
                        issues.append(_issue("schema_deprecated", "low", page_url,
                                             f"Structured data uses a deprecated/restricted type: {', '.join(sorted(bad))}"))
                # Guideline compliance — what Google actually enforces:
                ptypes = _schema_types(parsed)
                is_business = bool(ptypes & LOCALBUSINESS_SUBTYPES)
                if is_business:
                    lb_count["n"] += 1
                if ("schema_selfserving_reviews" not in trust_reported and is_business
                        and '"aggregaterating"' in raw.lower()):
                    trust_reported.add("schema_selfserving_reviews")
                    issues.append(_issue("schema_selfserving_reviews", "critical", page_url,
                                         "LocalBusiness schema injects its own aggregateRating (self-serving reviews) — "
                                         "ineligible per Google's review-snippet guidelines and a documented manual-action "
                                         "trigger"))
                if "schema_fake_address" not in trust_reported:
                    for m in re.finditer(r'"streetAddress"\s*:\s*"([^"]*)"', raw):
                        addr = m.group(1).strip()
                        if addr and (not re.search(r"\d", addr) or len(addr.split()) < 2):
                            trust_reported.add("schema_fake_address")
                            issues.append(_issue("schema_fake_address", "critical", page_url,
                                                 f"Schema streetAddress is “{addr}” — not a real street address. Without a "
                                                 "verifiable address there is no Google Business Profile, no citations, no "
                                                 "map-pack presence"))
                            break
                if ("no_entity_corroboration" not in trust_reported and is_business
                        and re.search(r'"sameAs"\s*:\s*\[\s*\]', raw)):
                    trust_reported.add("no_entity_corroboration")
                    issues.append(_issue("no_entity_corroboration", "medium", page_url,
                                         "Schema sameAs is empty — no Google Business Profile, directories, or social "
                                         "profiles corroborate the business entity anywhere"))

            if "duplicate_block" not in trust_reported:
                block_texts = {}
                for el in soup.find_all(["h2", "h3", "li"]):
                    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                    if 25 <= len(t) <= 200:
                        block_texts[t] = block_texts.get(t, 0) + 1
                dupb = [t for t, n in block_texts.items() if n >= 2]
                if len(dupb) >= 3:
                    trust_reported.add("duplicate_block")
                    issues.append(_issue("duplicate_block", "medium", page_url,
                                         f"{len(dupb)} content blocks render twice on the page (e.g. "
                                         f"\u201c{dupb[0][:50]}\u201d) \u2014 a template bug that duplicates copy"))

            if lb_count["n"] >= 2 and "schema_duplicate_entity" not in trust_reported:
                trust_reported.add("schema_duplicate_entity")
                issues.append(_issue("schema_duplicate_entity", "medium", page_url,
                                     f"{lb_count['n']} separate LocalBusiness schema entities on one page (not linked by "
                                     "@id) — conflicting signals about what the business is"))

            # --- security: mixed content on HTTPS pages ---
            if page_url.startswith("https://") and MIXED_CONTENT_RE.search(resp.text):
                issues.append(_issue("mixed_content", "medium", page_url,
                                     "Page loads some assets over insecure HTTP (mixed content)"))

            # --- trust layer: fabricated data is a ranking killer, not a detail ---
            if "fabricated_contact" not in trust_reported:
                fp = FAKE_PHONE_RE.search(soup.get_text(" ", strip=True))
                if fp:
                    trust_reported.add("fabricated_contact")
                    issues.append(_issue("fabricated_contact", "critical", page_url,
                                         f"Phone number {fp.group(0)} uses the fictional 555-01XX exchange — it cannot "
                                         "receive calls. A fake phone fails Google's business verification, kills local "
                                         "rankings, and loses every caller"))
            if "fabricated_credential" not in trust_reported:
                fl = FAKE_LICENSE_RE.search(soup.get_text(" ", strip=True))
                if fl:
                    trust_reported.add("fabricated_credential")
                    issues.append(_issue("fabricated_credential", "critical", page_url,
                                         f"Credential looks like a placeholder ({fl.group(0).strip()}) — presenting a "
                                         "fake license number is a legal and trust liability"))
            if "stock_images_hotlinked" not in trust_reported:
                stock = [i.get("src", "") for i in imgs
                         if any(h in (i.get("src") or "") for h in STOCK_HOSTS)]
                if len(stock) >= 3:
                    trust_reported.add("stock_images_hotlinked")
                    issues.append(_issue("stock_images_hotlinked", "high", page_url,
                                         f"{len(stock)} images are hotlinked stock photos (Unsplash/Pexels) presented as "
                                         "the business's work — Google and visitors can tell; real project photos are a "
                                         "core local ranking signal"))

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
                    linked_internal.add(link)
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
                        if internal:
                            int_links["checked"] += 1
                            if r.history:
                                int_links["redirected"] += 1
                                if not int_links["example"]:
                                    int_links["example"] = link
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
        home_soup = BeautifulSoup(homepage_html, "html.parser") if homepage_html else None

        # robots.txt (capture text for the AI-crawler check below)
        robots_txt = ""
        try:
            rr = client.get(origin + "/robots.txt")
            if rr.status_code >= 400:
                issues.append(_issue("indexation", "low", origin, "No robots.txt found"))
            else:
                robots_txt = rr.text
        except Exception:
            pass

        # sitemap existence + collect its URLs (for orphan detection)
        sitemap_urls: set[str] = set()
        sitemap_found = False
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"):
            try:
                sr = client.get(origin + path)
                if sr.status_code == 200 and "<" in sr.text[:200]:
                    sitemap_found = True
                    sitemap_urls = _sitemap_locs(client, sr.text)
                    break
            except Exception:
                pass
        if not sitemap_found:
            issues.append(_issue("indexation", "medium", origin,
                                 "No sitemap.xml found — search engines may miss pages"))

        # orphan pages: in the sitemap but never seen as an internal link. Only
        # trustworthy when the crawl EXHAUSTED all reachable links (didn't hit the
        # page cap); otherwise a page linked from an uncrawled page looks falsely
        # orphaned. So we skip orphan detection on sites larger than the cap.
        internal_sitemap = {u for u in sitemap_urls if _same_domain(u, start_url)}
        if internal_sitemap and pages_crawled < max_pages:
            reachable = {_normalize(u) for u in linked_internal} | {_normalize(u) for u in visited}
            orphans = [u for u in internal_sitemap
                       if _normalize(u) not in reachable and _normalize(u) != home_norm]
            for u in orphans[:5]:
                issues.append(_issue("orphan_page", "low", u,
                                     "Page is in the sitemap but not linked from any other page"))

        # thin archive pages declared indexable via the sitemap (tag/category/author)
        archives = [u for u in internal_sitemap if re.search(r"/(tag|category|author)/", u)]
        if len(archives) >= 5:
            issues.append(_issue("junk_archives", "medium", origin,
                                 f"{len(archives)} tag/category/author archive pages are in the sitemap — thin "
                                 f"auto-generated pages that dilute crawl budget (e.g. {archives[0]})"))

        # republished duplicates: /post-2/, /post-3/ slugs whose base post also exists
        sm_paths = {urlparse(u).path.rstrip("/") for u in internal_sitemap}
        dupes = [p for p in sm_paths
                 if re.search(r"-\d$", p) and re.sub(r"-\d$", "", p) in sm_paths]
        for p in sorted(dupes)[:3]:
            issues.append(_issue("duplicate_post", "medium", origin + p,
                                 f"Republished duplicate: {p} duplicates {re.sub(r'-\d$', '', p)} — the copies "
                                 "compete against each other (cannibalization); consolidate with a 301"))

        # internal links that all pass through 301 redirects (PageRank through hops)
        if int_links["checked"] >= 8 and int_links["redirected"] / int_links["checked"] >= 0.5:
            issues.append(_issue("internal_redirect_links", "medium", origin,
                                 f"{int_links['redirected']} of {int_links['checked']} internal links pass through a "
                                 f"301 redirect (e.g. {int_links['example']}) — every internal PageRank flow takes a "
                                 "detour; hrefs should point at the final URL"))

        # required pages (via discovered internal links)
        # Present only if the page actually loads (200). A required page that is
        # linked but 404s (e.g. a footer /privacy link with no page) is MISSING.
        for name, (keywords, sev) in REQUIRED_PAGES.items():
            if not any(any(k in p for k in keywords) for p in ok_paths):
                issues.append(_issue("required_page_missing", sev, origin,
                                     f"No {name} page found in the site's links"))

        # favicon
        if home_soup is not None and not home_soup.find("link", attrs={"rel": ICON_RE}):
            try:
                if client.get(origin + "/favicon.ico").status_code >= 400:
                    issues.append(_issue("missing_favicon", "low", origin, "No favicon found"))
            except Exception:
                pass

        # Open Graph / social-preview tags (homepage, template-representative)
        if home_soup is not None:
            missing_social = []
            for prop in ("og:title", "og:description", "og:image"):
                if not home_soup.find("meta", attrs={"property": prop}):
                    missing_social.append(prop)
            if not home_soup.find("meta", attrs={"name": "twitter:card"}):
                missing_social.append("twitter:card")
            if missing_social:
                issues.append(_issue("og_incomplete", "low", origin,
                                     "Missing social/preview tags: " + ", ".join(missing_social)))
            ogimg = home_soup.find("meta", attrs={"property": "og:image"})
            ogsrc = (ogimg.get("content") or "") if ogimg else ""
            if ogsrc and "stock_images_hotlinked" not in trust_reported and re.search(
                    r"pexels-photo|unsplash|pixabay|istockphoto|freepik", ogsrc, re.I):
                trust_reported.add("stock_images_hotlinked")
                issues.append(_issue("stock_images_hotlinked", "high", origin,
                                     f"The social-preview image (og:image) is a stock photo \u2014 the filename "
                                     f"still carries the stock ID ({ogsrc.rsplit('/', 1)[-1][:60]})"))

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

        # www-variant entry: should collapse to the canonical host in ONE hop
        try:
            bare = urlparse(start_url).netloc.removeprefix("www.")
            wr = client.get("http://www." + bare)
            if len(wr.history) >= 2:
                issues.append(_issue("redirect_issue", "low", "http://www." + bare,
                                     f"www entry takes {len(wr.history)} redirect hops to reach the canonical "
                                     "URL \u2014 collapse to a single hop at the server"))
        except Exception:
            pass

        # --- AI / GEO readiness ---
        blocked_ai = _ai_blocked(robots_txt)
        if blocked_ai:
            issues.append(_issue("ai_crawler_blocked", "medium", origin,
                                 "robots.txt blocks AI crawlers (" + ", ".join(blocked_ai) +
                                 ") — these pages can't be cited by AI search"))
        try:
            if client.get(origin + "/llms.txt").status_code >= 400:
                issues.append(_issue("no_llms_txt", "low", origin,
                                     "No llms.txt — the emerging standard for guiding AI assistants to your key content"))
        except Exception:
            pass

        home_types = _home_schema_types(home_soup)
        has_entity = (bool(home_types & ENTITY_SCHEMA) or any("business" in t for t in home_types)
                      or bool(home_types & LOCALBUSINESS_SUBTYPES))
        if home_soup is not None and not has_entity:
            issues.append(_issue("no_entity_schema", "medium", origin,
                                 "Homepage has no Organization/LocalBusiness entity schema — weakens AI and Knowledge Graph understanding of who you are"))

        # --- local SEO signals ---
        has_localbusiness = (any(t == "localbusiness" or "business" in t for t in home_types)
                             or bool(home_types & LOCALBUSINESS_SUBTYPES))
        if home_soup is not None and not has_localbusiness:
            issues.append(_issue("no_localbusiness_schema", "low", origin,
                                 "No LocalBusiness schema — important for local rankings and Google Business Profile alignment"))
        if home_soup is not None:
            has_tel = bool(home_soup.find("a", href=re.compile(r"^tel:", re.I)))
            if not has_tel and not PHONE_RE.search(home_soup.get_text(" ")):
                issues.append(_issue("nap_missing", "low", origin,
                                     "No phone number / click-to-call found on the homepage — hurts local trust and conversions"))

        # heading hierarchy (collapsed: one finding for the whole site)
        if heading_skip["count"]:
            n = heading_skip["count"]
            suffix = f" (on {n} pages)" if n > 1 else ""
            issues.append(_issue("heading_hierarchy", "low", heading_skip["url"],
                                 f"{heading_skip['detail']} — breaks the document outline{suffix}"))

        # duplicate titles
        seen: dict[str, list[str]] = {}
        for url, t in titles.items():
            seen.setdefault(t, []).append(url)
        for t, urls in seen.items():
            if len(urls) > 1:
                issues.append(_issue("duplicate_title", "medium", urls[0],
                                     f"Title \"{t[:60]}\" is duplicated on {len(urls)} pages"))

        # cannibalization: DIFFERENT pages whose titles chase the same query
        # (high token overlap after dropping brand/stopwords/years)
        stop = {"the", "and", "for", "your", "with", "what", "how", "much", "does",
                "cost", "guide", "tips", "2024", "2025", "2026", "idaho", "meridian"}
        tokset = {}
        for url, t in titles.items():
            toks = frozenset(w for w in re.findall(r"[a-z]{4,}", (t or "").lower()) if w not in stop)
            if len(toks) >= 3:
                tokset[url] = toks
        clustered: set = set()
        cl_urls = sorted(tokset)
        for i, u1 in enumerate(cl_urls):
            if u1 in clustered:
                continue
            group = [u1]
            for u2 in cl_urls[i + 1:]:
                if u2 in clustered:
                    continue
                a, b = tokset[u1], tokset[u2]
                if len(a & b) / max(1, len(a | b)) >= 0.6:
                    group.append(u2)
            if len(group) >= 3:
                clustered.update(group)
                issues.append(_issue("cannibalization", "medium", group[0],
                                     f"{len(group)} pages target the same query cluster (near-identical title "
                                     f"topics) \u2014 they compete against each other; consolidate or "
                                     f"differentiate (e.g. {', '.join(urlparse(g).path for g in group[:3])})"))

    stats = {"pages_crawled": pages_crawled, "links_checked": links_checked, "issues_found": len(issues)}
    return {"issues": issues, "stats": stats}
