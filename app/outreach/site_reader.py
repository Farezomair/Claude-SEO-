"""Read a prospect's website: sitemap URL list plus the pages that matter for a first email.

Only reads public pages. Never submits forms.
"""
import re
import time
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 20
MAX_PAGES = 10
PAGE_TEXT_LIMIT = 5000

# Pages worth reading, in priority order.
PAGE_HINTS = [
    ("about", r"about|our-story|who-we-are|mission"),
    ("team", r"team|staff|leadership|our-people|clinical-team|meet"),
    ("admissions", r"admission|get-started|intake|start-here"),
    ("insurance", r"insurance|verify|benefits|cost|pricing|payment|financ"),
    ("programs", r"program|services|treatment|levels-of-care|what-we-treat"),
    ("detox", r"detox|withdrawal"),
    ("residential", r"residential|inpatient"),
    ("outpatient", r"\biop\b|intensive-outpatient|outpatient|\bphp\b|partial-hosp"),
    ("locations", r"location|campus|facilit"),
    ("contact", r"contact"),
]


def _get(session, url):
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        return r
    except requests.RequestException:
        return None


def _norm(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/') or '/'}"


def _sitemap_urls(session, base, robots_text):
    candidates = re.findall(r"(?im)^sitemap:\s*(\S+)", robots_text or "")
    candidates += [urljoin(base, p) for p in ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")]
    seen, urls = set(), []
    queue = list(dict.fromkeys(candidates))
    while queue and len(seen) < 25:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = _get(session, sm)
        if not r or r.status_code != 200 or b"<" not in r.content[:200]:
            continue
        try:
            root = ElementTree.fromstring(r.content)
        except ElementTree.ParseError:
            continue
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text:
                t = loc.text.strip()
                if t.endswith(".xml") or "sitemap" in t.rsplit("/", 1)[-1]:
                    queue.append(t)
                else:
                    urls.append(t)
        if urls and not queue:
            break
    return list(dict.fromkeys(urls))[:1500]


def _page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": "description"})
    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    tels = sorted({a["href"][4:].strip() for a in soup.find_all("a", href=True) if a["href"].lower().startswith("tel:")})
    forms = len(soup.find_all("form"))
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    links = []
    for a in soup.find_all("a", href=True):
        links.append((urljoin(url, a["href"]), a.get_text(" ", strip=True)[:80]))
    return {
        "url": url,
        "title": title,
        "meta_description": md.get("content", "").strip() if md else "",
        "h1": h1,
        "tel_links": tels,
        "forms": forms,
        "text": text[:PAGE_TEXT_LIMIT],
        "full_text": text,
        "_links": links,
    }


def read_site(website: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    out = {"website": website, "reachable": False, "pages": [], "all_urls": [], "nav_links": [], "errors": []}
    r = _get(session, website)
    if not r or r.status_code >= 400:
        out["errors"].append(f"homepage {getattr(r, 'status_code', 'no response')}")
        return out
    out["reachable"] = True
    base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
    out["final_url"] = r.url
    home = _page(r.text, r.url)
    domain = urlparse(r.url).netloc.lower().removeprefix("www.")

    internal = []
    for href, label in home["_links"]:
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if host == domain and not re.search(r"\.(pdf|jpg|jpeg|png|gif|webp|zip)$", href, re.I):
            internal.append((_norm(href), label))
    out["nav_links"] = [{"url": u, "label": l} for u, l in dict(internal).items()][:150]

    robots = _get(session, urljoin(base, "/robots.txt"))
    out["all_urls"] = _sitemap_urls(session, base, robots.text if robots is not None and robots.status_code == 200 else "")

    pool = list(dict.fromkeys([u for u, _ in internal] + [_norm(u) for u in out["all_urls"]]))
    chosen = []
    for _, pattern in PAGE_HINTS:
        for u in pool:
            if u not in chosen and u != _norm(r.url) and re.search(pattern, urlparse(u).path, re.I):
                chosen.append(u)
                break
        if len(chosen) >= MAX_PAGES - 1:
            break

    pages = [home]
    for u in chosen:
        time.sleep(0.7)
        pr = _get(session, u)
        if pr is not None and pr.status_code < 400 and "text/html" in pr.headers.get("content-type", ""):
            if _norm(pr.url) in {_norm(p["url"]) for p in pages}:
                continue
            pages.append(_page(pr.text, pr.url))
        elif pr is not None:
            out["errors"].append(f"{u} -> {pr.status_code}")
    for p in pages:
        p.pop("_links", None)
    out["pages"] = pages
    return out


def corpus(site: dict) -> str:
    """Lowercased text of everything read, for verifying quotes and absence claims."""
    parts = [p["full_text"] for p in site.get("pages", [])]
    parts += site.get("all_urls", [])
    parts += [f"{n['url']} {n['label']}" for n in site.get("nav_links", [])]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def for_prompt(site: dict) -> dict:
    """Trimmed copy for the model (no full_text)."""
    return {
        "final_url": site.get("final_url"),
        "nav_links": site.get("nav_links", [])[:80],
        "sitemap_urls": site.get("all_urls", [])[:300],
        "pages": [{k: v for k, v in p.items() if k != "full_text"} for p in site.get("pages", [])],
        "errors": site.get("errors", []),
    }
