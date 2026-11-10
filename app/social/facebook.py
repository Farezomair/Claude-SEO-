"""Facebook Pages API (Graph API): connect a page, test it, publish posts.

Needs a Meta app the page admin created (free) with pages_show_list, pages_read_engagement and
pages_manage_posts. The owner pastes the app ID, app secret and a short-lived user token once; we
exchange it for a long-lived user token and read the page's token from /me/accounts, which does not
expire. Only the page token is stored, encrypted. The app secret is used once and not kept.
"""
import requests

from .. import crypto
from . import store

_TIMEOUT = 60


class FbError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _base():
    return "https://graph.facebook.com/" + store.load()["facebook"].get("graph_version", "v23.0")


def _call(method, path, **params):
    r = requests.request(method, _base() + path, data=params if method == "POST" else None,
                         params=params if method != "POST" else None, timeout=_TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        raise FbError(f"HTTP {r.status_code}: {r.text[:300]}")
    if isinstance(j, dict) and j.get("error"):
        e = j["error"]
        raise FbError(f"{e.get('message')} (code {e.get('code')}{', ' + str(e.get('error_subcode')) if e.get('error_subcode') else ''})",
                      e.get("code"))
    return j


def token():
    raw = store.get_meta("social_fb_token")
    return crypto.decrypt(raw) if raw else ""


def connect(app_id: str, app_secret: str, user_token: str, page_id: str = "") -> dict:
    """Exchange the user token and store the page token. Returns {page_id, page_name}."""
    ll = _call("GET", "/oauth/access_token", grant_type="fb_exchange_token", client_id=app_id.strip(),
               client_secret=app_secret.strip(), fb_exchange_token=user_token.strip())["access_token"]
    pages = _call("GET", "/me/accounts", access_token=ll, fields="id,name,access_token,tasks", limit=100).get("data", [])
    if not pages:
        raise FbError("This Facebook account manages no pages the app can see. In the login dialog, select the page.")
    want = page_id.strip() or store.load()["facebook"].get("page_id", "")
    pg = next((p for p in pages if p["id"] == want), None) if want else (pages[0] if len(pages) == 1 else None)
    if not pg:
        raise FbError("Several pages found; enter the page ID. Pages: " + ", ".join(f"{p['name']} ({p['id']})" for p in pages))
    if "CREATE_CONTENT" not in (pg.get("tasks") or []):
        raise FbError(f"Your account can't create content on {pg['name']}.")
    store.set_meta("social_fb_token", crypto.encrypt(pg["access_token"]))
    store.save_patch({"facebook": {"page_id": pg["id"], "page_name": pg["name"]}})
    return {"page_id": pg["id"], "page_name": pg["name"]}


def connect_page_token(page_token: str) -> dict:
    """Store a page token the owner already has (it must belong to a page)."""
    me = _call("GET", "/me", access_token=page_token.strip(), fields="id,name")
    store.set_meta("social_fb_token", crypto.encrypt(page_token.strip()))
    store.save_patch({"facebook": {"page_id": me["id"], "page_name": me["name"]}})
    return {"page_id": me["id"], "page_name": me["name"]}


def test(image_url: str = "") -> list[str]:
    """Check the token works and can publish, without anything appearing on the page:
    create an unpublished photo (or post), read it back, delete it. Returns step results."""
    t, pid = token(), store.load()["facebook"]["page_id"]
    if not t or not pid:
        raise FbError("Not connected.")
    steps = []
    me = _call("GET", "/me", access_token=t, fields="id,name")
    steps.append(f"token belongs to the page {me['name']} ({me['id']})")
    caption = "Ascend connection test, not published."
    if image_url:
        r = _call("POST", f"/{pid}/photos", access_token=t, url=image_url, caption=caption, published="false")
        oid = r["id"]
        got = _call("GET", f"/{oid}", access_token=t, fields="name")
        steps.append("unpublished photo created" + (", caption saved" if got.get("name") == caption else ", CAPTION MISSING"))
    else:
        r = _call("POST", f"/{pid}/feed", access_token=t, message=caption, published="false")
        oid = r["id"]
        steps.append("unpublished post created")
    _call("DELETE", f"/{oid}", access_token=t)
    steps.append("test item deleted")
    return steps


def publish(message: str, image_url: str = "", link: str = "") -> dict:
    """Publish now. Photo post (text + link in the caption) when there is a picture, else a link post."""
    t, pid = token(), store.load()["facebook"]["page_id"]
    if not t or not pid:
        raise FbError("Not connected.")
    text = message.strip() + (f"\n\n{link}" if link else "")
    if image_url:
        r = _call("POST", f"/{pid}/photos", access_token=t, url=image_url, caption=text, published="true")
        post_id = r.get("post_id") or r["id"]
    else:
        r = _call("POST", f"/{pid}/feed", access_token=t, message=message.strip(), link=link)
        post_id = r["id"]
    try:
        permalink = _call("GET", f"/{post_id}", access_token=t, fields="permalink_url").get("permalink_url", "")
    except FbError:
        permalink = ""
    return {"post_id": post_id, "permalink": permalink}
