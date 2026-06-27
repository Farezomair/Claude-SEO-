"""WordPress REST API client, targeting Yoast SEO meta fields.

Read-only and write operations go through the standard WP REST API using an
application password (HTTP Basic auth). Writing Yoast meta requires the site to
have the SEO Agent Connector helper plugin installed, which exposes these
protected meta keys to REST. See wordpress-plugin/seo-agent-connector.php.
"""
import httpx
from bs4 import BeautifulSoup

# Yoast SEO meta keys.
YOAST_TITLE_KEY = "_yoast_wpseo_title"
YOAST_DESC_KEY = "_yoast_wpseo_metadesc"

REQUEST_TIMEOUT = 20.0
USER_AGENT = "SEO-Agent/1.0"


class WordPressError(Exception):
    pass


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str):
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/")
        # WordPress accepts the application password with or without spaces.
        self.auth = (username, app_password)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=REQUEST_TIMEOUT,
            auth=self.auth,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def test(self) -> tuple[bool, int]:
        """Verify the credentials by fetching the authenticated user."""
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/wp-json/wp/v2/users/me", params={"context": "edit"})
            return r.status_code == 200, r.status_code
        except Exception:
            return False, 0

    def list_content(self, kinds=("pages", "posts"), limit: int = 60) -> list[dict]:
        """Return published pages/posts with their title, content text, and Yoast meta."""
        items: list[dict] = []
        with self._client() as c:
            for kind in kinds:
                page = 1
                while len(items) < limit:
                    r = c.get(
                        f"{self.base}/wp-json/wp/v2/{kind}",
                        params={
                            "per_page": 50,
                            "page": page,
                            "status": "publish",
                            "context": "edit",
                            "_fields": "id,link,title,meta,content",
                        },
                    )
                    if r.status_code != 200:
                        break
                    batch = r.json()
                    if not batch:
                        break
                    for it in batch:
                        items.append(self._normalize(kind, it))
                    if len(batch) < 50:
                        break
                    page += 1
        return items[:limit]

    def get_meta(self, kind: str, item_id: int) -> dict:
        with self._client() as c:
            r = c.get(
                f"{self.base}/wp-json/wp/v2/{kind}/{item_id}",
                params={"context": "edit", "_fields": "id,meta"},
            )
        if r.status_code != 200:
            raise WordPressError(f"HTTP {r.status_code} reading meta")
        return (r.json() or {}).get("meta", {}) or {}

    def update_meta(self, kind: str, item_id: int, meta: dict) -> None:
        with self._client() as c:
            r = c.post(f"{self.base}/wp-json/wp/v2/{kind}/{item_id}", json={"meta": meta})
        if r.status_code not in (200, 201):
            raise WordPressError(f"HTTP {r.status_code}: {r.text[:200]}")

    def get_custom_css(self) -> str:
        """Read the site's Additional CSS via the helper plugin endpoint."""
        with self._client() as c:
            r = c.get(f"{self.base}/wp-json/seo-agent/v1/custom-css")
        if r.status_code == 404:
            raise WordPressError("CSS endpoint not found — update the helper plugin to v1.1 on this site.")
        if r.status_code != 200:
            raise WordPressError(f"HTTP {r.status_code} reading custom CSS")
        return (r.json() or {}).get("css", "") or ""

    def update_custom_css(self, css: str) -> None:
        """Replace the site's Additional CSS via the helper plugin endpoint."""
        with self._client() as c:
            r = c.post(f"{self.base}/wp-json/seo-agent/v1/custom-css", json={"css": css})
        if r.status_code == 404:
            raise WordPressError("CSS endpoint not found — update the helper plugin to v1.1 on this site.")
        if r.status_code not in (200, 201):
            raise WordPressError(f"HTTP {r.status_code}: {r.text[:200]}")

    def create_post(self, title: str, content_html: str, status: str = "draft", excerpt: str = "") -> dict:
        """Create a blog post. status 'draft' keeps it private until published in WP."""
        return self._create("posts", title, content_html, status, excerpt)

    def create_page(self, title: str, content_html: str, status: str = "draft") -> dict:
        """Create a WordPress page (e.g. a missing privacy/about page) as a draft."""
        return self._create("pages", title, content_html, status, "")

    def _create(self, kind: str, title: str, content_html: str, status: str, excerpt: str) -> dict:
        payload = {"title": title, "content": content_html, "status": status}
        if excerpt:
            payload["excerpt"] = excerpt
        with self._client() as c:
            r = c.post(f"{self.base}/wp-json/wp/v2/{kind}", json=payload)
        if r.status_code not in (200, 201):
            raise WordPressError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json() or {}
        return {"id": data.get("id"), "link": data.get("link", ""), "status": data.get("status", status)}

    def update_content(self, kind: str, item_id: int, content_html: str) -> None:
        """Replace a post/page's content body (used by the Content Corrector)."""
        with self._client() as c:
            r = c.post(f"{self.base}/wp-json/wp/v2/{kind}/{item_id}", json={"content": content_html})
        if r.status_code not in (200, 201):
            raise WordPressError(f"HTTP {r.status_code}: {r.text[:200]}")

    @staticmethod
    def _normalize(kind: str, item: dict) -> dict:
        title = ((item.get("title") or {}).get("rendered") or "").strip()
        html = (item.get("content") or {}).get("rendered") or ""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return {
            "kind": kind,
            "id": item.get("id"),
            "link": item.get("link") or "",
            "title": title,
            "meta": item.get("meta") or {},
            "content_text": text[:1800],
            "content_html": html,
        }
