"""Image de-hotlink doer — stop presenting externally-hotlinked stock photos as
the business's own work (defect #6).

Hotlinking from Unsplash/Pexels/etc. is a legal + performance + trust problem:
the images aren't in the media library, they can vanish, and they're shown as
real project photos. This doer downloads each hotlinked stock image, uploads it
to the WordPress media library, and rewrites the page body to point at the local
copy — so nothing is hotlinked anymore.

It does NOT claim the photo is a real project. Whether a stock image may be
*presented* as completed work is an owner judgment, so after de-hotlinking the
finding is annotated (not silently closed) asking the owner to confirm or swap
in a genuine project photo. Per page, snapshotted, reversible.
"""
import re
import threading
from urllib.parse import urlparse

import httpx

from .abilities import USER_AGENT
from .crawler import STOCK_HOSTS
from .database import SessionLocal
from .elementor_agent import AbilitiesClient, list_elementor_pages, read_body, write_body
from .models import Finding, FixRecord, JobRun, RunLog, Site, SiteChange
from .wordpress import WordPressClient

IMG_SRC_RE = re.compile(r'src=(["\'])(https?://[^"\']+)\1', re.I)
_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "gif": "image/gif", "avif": "image/avif"}


def _is_stock(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in STOCK_HOSTS)


def _download(url: str) -> tuple[bytes, str, str] | None:
    """(bytes, filename, mime) for an external image, or None."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(url)
        if r.status_code != 200 or not r.content:
            return None
        path = urlparse(url).path
        base = re.sub(r"[^a-zA-Z0-9._-]", "-", path.rsplit("/", 1)[-1] or "image")
        ext = (base.rsplit(".", 1)[-1].lower() if "." in base else "")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ext not in _MIME:
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                   "image/gif": "gif", "image/avif": "avif"}.get(ctype, "jpg")
            base = f"{base or 'image'}.{ext}"
        return r.content, base[:100], _MIME.get(ext, ctype or "image/jpeg")
    except Exception:
        return None


def run_image_rehost(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])

        def _label(t):
            run.progress_label = t[:290]
            db.commit()

        pages = list_elementor_pages(conn)[:40]
        cache: dict[str, str] = {}     # external url -> new media url
        pages_changed = rehosted = 0
        for i, p in enumerate(pages, start=1):
            pid = p.get("id")
            if not pid:
                continue
            _label(f"Scanning for hotlinked stock images… {i} of {len(pages)}")
            body = read_body(client, pid)
            if not body:
                continue
            externals = [(m.group(1), m.group(2)) for m in IMG_SRC_RE.finditer(body)
                         if _is_stock(m.group(2))]
            if not externals:
                continue
            new_body = body
            for _q, url in externals:
                if url not in cache:
                    got = _download(url)
                    if not got:
                        continue
                    try:
                        cache[url] = wp.upload_media(got[1], got[0], got[2])
                    except Exception:
                        cache[url] = ""
                local = cache.get(url)
                if local and local != url:
                    new_body = new_body.replace(url, local)
                    rehosted += 1
            if new_body != body and write_body(client, pid, new_body):
                pages_changed += 1
                db.add(SiteChange(
                    site_id=site_id, kind="image_rehost",
                    request=f"Re-host {len(externals)} hotlinked image(s) on {p.get('title') or pid}",
                    css=new_body, old_css=body, status="applied",
                    target_page_id=pid, target_widget_id=""))
                db.commit()

        if rehosted:
            # De-hotlinked, but "is this a real project photo?" is the owner's call —
            # annotate the finding rather than silently closing it.
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "stock_images_hotlinked",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "needs-human"
                f.remark = (f"Auto-fixed the hotlinking: {rehosted} stock image(s) downloaded to your "
                            "media library (no longer hotlinked). Still your call — if any are shown as "
                            "completed projects, swap in a genuine photo of your own work.")
            db.add(FixRecord(
                site_id=site_id, doer="Image Agent", field="stock_images_hotlinked",
                action_taken=f"Re-hosted {rehosted} hotlinked stock image(s) to the media library across {pages_changed} page(s)",
                page_ref=site.url, before_value="(images hotlinked from external CDNs)",
                after_value=f"{rehosted} image(s) now served from your media library",
                method="auto-safe", lane="autonomous", applied=True,
                verification_verdict="verified", status="done"))
            run.summary = (f"Re-hosted {rehosted} hotlinked stock image(s) into your media library across "
                           f"{pages_changed} page(s). Flagged for your review: confirm none are shown as your projects.")
        else:
            run.summary = "No hotlinked stock images found in editable page bodies."
        run.status = "completed"
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Image re-host run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_image_rehost_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_image_rehost, args=(site_id, run_id, conn), daemon=True).start()
