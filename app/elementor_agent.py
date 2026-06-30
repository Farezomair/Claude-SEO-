"""Elementor On-page agent — full-page SEO rewrites via the Abilities API.

Every Meridian page is a single Elementor `html` widget holding a complete HTML
document (see docs/abilities-catalog.md). This agent reads that page body, has
Claude rewrite the visible copy for search intent while preserving structure,
CSS, and scripts, runs safety checks, and routes the change to the approval gate
with a saved snapshot for one-click rollback. Nothing touches the live page until
the owner approves — and approval/revert both go through `apply_html`.
"""
import difflib
import json
import threading

import httpx
from bs4 import BeautifulSoup

from .abilities import AbilitiesClient, AbilitiesError, AbilitiesUnavailable, USER_AGENT
from .brain import rewrite_page_html
from .content_standard import scan, strip_em_dashes
from .database import SessionLocal
from .models import Approval, FixRecord, JobRun, RunLog, Site, SiteChange
from .rules import rules_for
from .wordpress import WordPressClient

# Bound concurrent full-page rewrites: each is a large streaming Claude call, so a
# whole-site sweep fired at once would hit API rate limits. Process a few at a time.
_REWRITE_SEM = threading.Semaphore(3)

P = "hostinger-ai-assistant"
A_FIND = f"{P}/elementor-find-widgets"
A_UPDATE_CONTENT = f"{P}/elementor-update-widget-content"
A_PAGE_GET = f"{P}/pages-get"
A_PAGE_UPDATE = f"{P}/pages-update"
A_CACHE_FLUSH = f"{P}/litespeed-cache-flush"


# -- reads -------------------------------------------------------------------
def list_elementor_pages(conn: dict) -> list[dict]:
    """Published pages for the Website tab (best-effort).

    There is no `elementor-list-pages` ability (it isn't in the catalog), so we
    list published pages through the standard WordPress REST adapter instead —
    every Meridian page is an Elementor page, and this is the reliable source.
    """
    try:
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
        return [{"id": it["id"], "title": it.get("title", "")}
                for it in wp.list_content(kinds=("pages",), limit=50) if it.get("id")]
    except Exception:
        return []


def _find_html_widget(client: AbilitiesClient, page_id: int) -> tuple[str, str]:
    """Return (widget_id, current_html) for the page's html widget, or ('', '')."""
    res = client.read(A_FIND, {"post_id": page_id, "widget_types": ["html"], "include_settings": True})
    for w in (res or {}).get("widgets") or []:
        if w.get("widget_type") == "html":
            return w.get("id") or "", ((w.get("settings") or {}).get("html") or "")
    return "", ""


# -- writes (shared by approve + revert) -------------------------------------
def _set_widget_html(nodes, widget_id: str, html: str) -> bool:
    """Recursively set settings.html on the matching node in _elementor_data."""
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        if isinstance(n, dict):
            if n.get("id") == widget_id:
                n.setdefault("settings", {})["html"] = html
                return True
            if _set_widget_html(n.get("elements", []), widget_id, html):
                return True
    return False


def _elementor_data_of(page: dict) -> str:
    """Read _elementor_data from a pages-get result, tolerating shape variants
    (under `meta`, or at the top level)."""
    page = page or {}
    meta = page.get("meta") or {}
    return meta.get("_elementor_data") or page.get("_elementor_data") or ""


def _apply_via_elementor_data(client: AbilitiesClient, page_id: int, widget_id: str, html: str) -> None:
    """Surgically set ONE widget's settings.html inside _elementor_data and save.

    This is the real write path for Meridian's single-html-widget pages, because
    the catalog's `elementor-update-widget-content` only edits text/heading/button
    widgets (it no-ops on `html` widgets)."""
    page = client.read(A_PAGE_GET, {"id": page_id})
    data_str = _elementor_data_of(page)
    if not data_str:
        raise AbilitiesError("Could not read _elementor_data to edit the page body.")
    try:
        data = json.loads(data_str)
    except (ValueError, TypeError) as exc:
        raise AbilitiesError(f"Page Elementor data is not valid JSON; not editing: {exc}")
    if not _set_widget_html(data, widget_id, html):
        raise AbilitiesError(f"Widget {widget_id} not found in the page's Elementor data.")
    client.run(A_PAGE_UPDATE, {"id": page_id, "meta": {"_elementor_data": json.dumps(data, ensure_ascii=False)}})


def read_body(client: AbilitiesClient, page_id: int) -> str | None:
    """Read the `_meridian_body` custom field that the child theme's
    meridian-full-page.php template actually prints on the front end. Returns the
    HTML string, '' if the field is empty, or None if the endpoint isn't available."""
    url = f"{client.base}/wp-json/seo-agent/v1/body"
    try:
        with httpx.Client(timeout=45.0, auth=client.auth,
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.get(url, params={"post_id": page_id, "include_html": 1})
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        return (r.json() or {}).get("html")
    except Exception:
        return None


def write_body(client: AbilitiesClient, page_id: int, html: str) -> bool:
    """Write `_meridian_body` (the live render source) and purge. Returns True only
    if the plugin confirms the stored value matches what we sent."""
    url = f"{client.base}/wp-json/seo-agent/v1/body"
    try:
        with httpx.Client(timeout=60.0, auth=client.auth,
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(url, json={"post_id": page_id, "html": html})
    except Exception:
        return False
    if r.status_code not in (200, 201):
        return False
    try:
        return bool((r.json() or {}).get("verified"))
    except Exception:
        return False


def plugin_set_widget(client: AbilitiesClient, page_id: int, widget_id: str, html: str) -> bool:
    """Write the widget's html via the SEO Agent Bridge helper plugin, which edits
    `_elementor_data` in PHP (the Abilities/REST API can't) and verifies + purges
    cache server-side. Returns True only if the plugin confirms the change is live;
    False if the plugin isn't installed (404) or the write didn't verify, so callers
    can fall back to the ability paths."""
    url = f"{client.base}/wp-json/seo-agent/v1/elementor"
    try:
        with httpx.Client(timeout=45.0, auth=client.auth,
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(url, json={"post_id": page_id, "widget_id": widget_id, "html": html})
    except Exception:
        return False
    if r.status_code not in (200, 201):
        return False
    try:
        return bool((r.json() or {}).get("verified"))
    except Exception:
        return False


def apply_html(client: AbilitiesClient, page_id: int, widget_id: str, html: str,
               old_html: str | None = None) -> str:
    """Write HTML into the page's html widget. Returns the method actually used.

    Preferred path: the SEO Agent Bridge helper plugin, which edits `_elementor_data`
    in PHP — the only thing that actually lands on these single-html-widget pages
    (the Abilities API can't: `elementor-update-widget-content` silently no-ops on
    html widgets and `pages-get` won't return `_elementor_data` to edit). If the
    plugin isn't installed, fall back to the ability paths: widget-content with a
    landed-check, then a surgical `_elementor_data` edit. Both are reversible via the
    caller's snapshot.

    Pass `old_html` so the landed-check can look for the precise change (not just a
    head sample that never moves); without it we sample the body.
    """
    if plugin_set_widget(client, page_id, widget_id, html):
        # Trigger a native page save so Hostinger/LiteSpeed auto-purges this page's
        # server cache (the manual flush ability is unreliable / 500s here).
        try:
            client.run(A_PAGE_UPDATE, {"id": page_id})
        except (AbilitiesError, AbilitiesUnavailable):
            pass
        try:
            client.run(A_CACHE_FLUSH, {})
        except (AbilitiesError, AbilitiesUnavailable):
            pass
        return "plugin"
    method = "widget-content"
    landed = False
    try:
        client.run(A_UPDATE_CONTENT, {"post_id": page_id, "widget_id": widget_id, "content": html})
        landed = _change_is_live(client, page_id, html, old_html)
    except (AbilitiesError, AbilitiesUnavailable):
        landed = False
    if not landed:
        method = "elementor-data"
        _apply_via_elementor_data(client, page_id, widget_id, html)
    try:
        client.run(A_CACHE_FLUSH, {})
    except (AbilitiesError, AbilitiesUnavailable):
        pass
    return method


def _distinct_fragments(old_html: str, new_html: str, k: int = 3, minlen: int = 20) -> list[str]:
    """Up to k substrings present in new_html but NOT in old_html — fingerprints of
    the change, used to prove it actually landed on the live page."""
    old_html, new_html = old_html or "", new_html or ""
    if len(old_html) > 200000 or len(new_html) > 200000:
        # Too large to diff cheaply: use a trailing chunk, but only if it's genuinely
        # new (a shared/unchanged tail is no proof the change landed).
        tail = new_html.strip()[-200:]
        return [tail] if len(tail) >= minlen and tail not in old_html else []
    frags: list[str] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_html, new_html, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            seg = new_html[j1:j2].strip()
            if len(seg) >= minlen:
                frags.append(seg[:120])
    frags.sort(key=len, reverse=True)
    return frags[:k]


def _change_is_live(client: AbilitiesClient, page_id: int,
                    new_html: str, old_html: str | None = None) -> bool:
    """Re-read the page's html widget and decide whether the change is actually live.

    Robust against the two failure modes the old check missed: (1) an unchanged page
    falsely passing because only the first 400 chars were sampled, and (2) a real but
    deep/appended change (image dims mid-document, schema appended at the end) never
    being looked at."""
    try:
        _wid, live = _find_html_widget(client, page_id)
    except (AbilitiesError, AbilitiesUnavailable):
        return False
    if not live:
        return False
    live_s, new_s = live.strip(), (new_html or "").strip()
    if not new_s:
        return False
    if live_s == new_s:
        return True
    if old_html is not None and live_s == (old_html or "").strip():
        return False  # still exactly the old content — definitively did not land
    frags = _distinct_fragments(old_html or "", new_s)
    if frags:
        return all(f in live for f in frags)
    # No baseline / no detectable additions: sample the body, not just the head.
    mid = new_s[len(new_s) // 2: len(new_s) // 2 + 200].strip() or new_s[:200].strip()
    return bool(mid) and mid in live


def verify_change(client: AbilitiesClient, page_id: int,
                  old_html: str, new_html: str) -> bool:
    """True if the page's html widget now reflects new_html (given it was old_html)."""
    return _change_is_live(client, page_id, new_html, old_html)


def verify_html(client: AbilitiesClient, page_id: int, expected_html: str) -> bool:
    """Back-compat shim: confirm expected_html is live (no baseline available)."""
    return _change_is_live(client, page_id, expected_html, None)


# -- copy diff (for the approval) --------------------------------------------
def _visible_lines(html: str) -> list[str]:
    """Human-visible text lines of a page, with style/script/markup stripped."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["style", "script", "noscript"]):
        t.decompose()
    return [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]


def copy_diff(old_html: str, new_html: str, max_lines: int = 500) -> list[tuple[str, str]]:
    """Return [(kind, text), ...] where kind is 'add' | 'del' | 'ctx', diffing the
    VISIBLE COPY (not the markup) so the owner can read exactly what wording
    changed alongside the visual preview."""
    old, new = _visible_lines(old_html), _visible_lines(new_html)
    out: list[tuple[str, str]] = []
    for line in difflib.unified_diff(old, new, lineterm="", n=1):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            out.append(("add", line[1:].strip()))
        elif line.startswith("-"):
            out.append(("del", line[1:].strip()))
        else:
            out.append(("ctx", line.strip()))
        if len(out) >= max_lines:
            out.append(("ctx", "… (diff truncated)"))
            break
    return out


# -- safety checks -----------------------------------------------------------
def validate_rewrite(old_html: str, new_html: str) -> list[str]:
    """Flag anything that suggests the rewrite could break the live page."""
    flags = []
    if not new_html or len(new_html) < 0.5 * len(old_html):
        flags.append("Rewrite looks truncated (much shorter than the original).")
    if "<style" in old_html and "<style" not in new_html:
        flags.append("Missing <style> block — page styling could break.")
    if "<script" in old_html and "<script" not in new_html:
        flags.append("Missing <script> block — interactive parts (e.g. FAQ toggles) could break.")
    old_links, new_links = old_html.count("href="), new_html.count("href=")
    if old_links and new_links < 0.7 * old_links:
        flags.append(f"Lost links ({new_links} vs {old_links} originally).")
    old_imgs, new_imgs = old_html.count("<img"), new_html.count("<img")
    if old_imgs and new_imgs < old_imgs:
        flags.append(f"Lost images ({new_imgs} vs {old_imgs} originally).")
    sc = scan(new_html)
    if sc.get("em_dashes"):
        flags.append("Contained em dashes (auto-stripped).")
    if sc.get("banned"):
        flags.append("Contains banned words: " + ", ".join(sc["banned"][:5]))
    return flags


# -- the run -----------------------------------------------------------------
def run_page_rewrite(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        if not client.available():
            run.status = "failed"
            run.summary = "The site's Abilities API is not reachable. Check the connection."
            db.commit()
            return

        # Read the LIVE render source: the _meridian_body field the theme prints.
        old_html = read_body(client, page_id)
        if old_html is None:
            run.status = "failed"
            run.summary = "Couldn't read the page body — is SEO Agent Bridge (v4+) active?"
            db.commit()
            return
        if not old_html:
            run.status = "failed"
            run.summary = f"Page {page_id} body is empty."
            db.commit()
            return
        widget_id = ""

        try:
            with _REWRITE_SEM:  # cap concurrent rewrites to avoid API rate limits
                result = rewrite_page_html(site.name, site.url, page_title or str(page_id),
                                           old_html, rules_for("shared", "website"))
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Rewrite generation failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return

        new_html = strip_em_dashes(result.get("html", ""))
        if not new_html:
            run.status = "failed"
            run.summary = "The model returned no HTML."
            db.commit()
            return

        flags = validate_rewrite(old_html, new_html)
        change = SiteChange(
            site_id=site_id, kind="page_rewrite",
            request=f"SEO rewrite: {page_title or ('page ' + str(page_id))}",
            css=new_html, old_css=old_html, status="proposed",
            target_page_id=page_id, target_widget_id="",
        )
        db.add(change)
        db.commit()
        db.refresh(change)
        summary = result.get("summary", "SEO rewrite of the page copy.")

        if flags:
            # Safety checks flagged something (truncation / lost structure / banned
            # words) — gate this one for human review rather than auto-applying.
            db.add(Approval(
                site_id=site_id, kind="page_rewrite",
                title=f"SEO rewrite: {page_title or ('page ' + str(page_id))}",
                summary=summary + "  ⚠ Review before approving: " + " ".join(flags),
                payload=json.dumps({"change_id": change.id, "page_id": page_id, "flags": flags}),
                status="pending",
            ))
            run.status = "completed"
            run.summary = f"Proposed an SEO rewrite of '{page_title or page_id}' — needs review ({len(flags)} flag(s))."
        else:
            # Clean rewrite — auto-apply to the live body (revertible).
            ok = write_body(client, page_id, new_html)
            change.status = "applied" if ok else "failed"
            db.add(FixRecord(
                site_id=site_id, doer="Elementor On-page", field="page_html",
                action_taken=f"Auto-applied SEO rewrite of '{page_title or page_id}' (via _meridian_body)",
                page_ref=str(page_id), before_value=(old_html or "")[:5000], after_value=(new_html or "")[:5000],
                method="auto-safe", lane="autonomous", applied=bool(ok),
                verification_verdict="verified" if ok else "not_fixed", status="done", outcome_pending=True,
            ))
            run.status = "completed"
            run.summary = (f"Auto-applied SEO rewrite of '{page_title or page_id}' — live ({summary})"
                           if ok else f"Rewrite of '{page_title or page_id}' didn't verify.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_page_rewrite_async(site_id: int, run_id: int, conn: dict,
                             page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_page_rewrite,
                     args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
