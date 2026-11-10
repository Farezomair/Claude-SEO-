"""Read the site: published blog posts (public WordPress REST API) and service pages (HTML)."""
import html as _html
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; AscendSocial/1.0)"
_TIMEOUT = 30


def _get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=_TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _text_and_images(content_html: str, exclude_words=()) -> tuple[str, list]:
    soup = BeautifulSoup(content_html or "", "html.parser")
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    images, seen = [], set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").split("?")[0]
        alt = (img.get("alt") or "").strip()
        if not src or src in seen or not re.search(r"\.(png|jpe?g|webp)$", src, re.I):
            continue
        if re.search(r"-\d+x\d+\.(png|jpe?g|webp)$", src, re.I):  # resized copy
            continue
        if any(w.lower() in alt.lower() for w in exclude_words):
            continue
        seen.add(src)
        images.append({"url": src, "alt": alt})
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    return text, images


def blog_posts(site: str, exclude_words=(), limit=50) -> list[dict]:
    """Published posts, newest first: url, title, published (UTC datetime), text, images."""
    url = site.rstrip("/") + "/wp-json/wp/v2/posts"
    out = []
    data = _get(url, params={"per_page": min(limit, 100), "status": "publish",
                             "_fields": "id,date_gmt,link,title,content,excerpt"}).json()
    for p in data:
        text, images = _text_and_images(p["content"]["rendered"], exclude_words)
        out.append({
            "id": p["id"], "url": p["link"], "title": _html.unescape(BeautifulSoup(p["title"]["rendered"], "html.parser").get_text()),
            "published": datetime.fromisoformat(p["date_gmt"]).replace(tzinfo=timezone.utc),
            "text": text, "images": images, "type": "post",
        })
    return out


def page(url: str, exclude_words=()) -> dict:
    """A service or hub page: main content text, its title and its share image."""
    r = _get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.string if soup.title else url)
    og = (soup.find("meta", property="og:image") or {}).get("content", "")
    main = soup.find("main") or soup.body or soup
    for tag in main.find_all(["header", "footer", "nav"]):
        tag.decompose()
    text, images = _text_and_images(str(main), exclude_words)
    title = re.split(r"\s+[|–—-]\s+", _html.unescape(title).strip())[0]
    return {"url": url, "title": title, "text": text, "images": images,
            "og_image": og, "type": "page"}
