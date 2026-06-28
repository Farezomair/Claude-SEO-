"""Image-dimensions doer — kills the Core Web Vitals (CLS) findings.

The pages are single Elementor html widgets whose hand-built `<img>` tags lack
width/height, which causes layout shift. This measures each image's real intrinsic
size (a tiny dependency-free sniffer for JPEG/PNG/GIF/WebP) and injects width/height
attributes via byte-preserving regex (no other markup touched, no visual change —
CSS still controls displayed size). Gated like the other page edits; reuses the
Elementor apply/verify/revert path.
"""
import re
import struct
import threading
from urllib.parse import urljoin

import httpx

from .abilities import AbilitiesError, AbilitiesUnavailable
from .database import SessionLocal
from .elementor_agent import _find_html_widget, AbilitiesClient
from .models import Approval, JobRun, RunLog, Site, SiteChange

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)
# Require a leading space so `data-width=` / `data-height=` (and CSS `max-width:`)
# don't count as a real width/height attribute — otherwise we'd skip images that
# actually still need dimensions and the CLS fix would be a silent no-op.
HAS_W = re.compile(r"\swidth\s*=", re.I)
HAS_H = re.compile(r"\sheight\s*=", re.I)
MAX_IMAGES = 8          # bounded so a page's images can't stall the run
FETCH_BYTES = 200_000
PER_IMAGE_TIMEOUT = 4.0


def _img_size(data: bytes):
    """Intrinsic (width, height) from the leading bytes of common image formats."""
    if len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fmt = data[12:16]
        try:
            if fmt == b"VP8 ":
                return (struct.unpack("<H", data[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", data[28:30])[0] & 0x3FFF)
            if fmt == b"VP8L":
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                return (((b1 & 0x3F) << 8 | b0) + 1,
                        ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1)
            if fmt == b"VP8X":
                return ((data[24] | data[25] << 8 | data[26] << 16) + 1,
                        (data[27] | data[28] << 8 | data[29] << 16) + 1)
        except Exception:
            return None
    if data[:2] == b"\xff\xd8":  # JPEG: scan SOF markers
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = struct.unpack(">H", data[i + 5:i + 7])[0]
                w = struct.unpack(">H", data[i + 7:i + 9])[0]
                return w, h
            if i + 4 > n:
                break
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
    return None


def _missing_dims_srcs(html: str) -> list[str]:
    srcs = []
    for tag in IMG_RE.findall(html):
        if HAS_W.search(tag) and HAS_H.search(tag):
            continue
        m = SRC_RE.search(tag)
        if m and m.group(1) not in srcs:
            srcs.append(m.group(1))
    return srcs[:MAX_IMAGES]


def _add_dims(html: str, sizes: dict) -> str:
    def repl(m):
        tag = m.group(0)
        if HAS_W.search(tag) and HAS_H.search(tag):
            return tag
        sm = SRC_RE.search(tag)
        if not sm or sm.group(1) not in sizes:
            return tag
        w, h = sizes[sm.group(1)]
        return re.sub(r"\s*/?>\s*$", f' width="{w}" height="{h}">', tag, count=1)
    return IMG_RE.sub(repl, html)


def _measure(base_url: str, srcs: list[str]) -> dict:
    sizes = {}
    headers = {"User-Agent": "SEO-Agent/1.0"}
    with httpx.Client(timeout=PER_IMAGE_TIMEOUT, follow_redirects=True, headers=headers) as c:
        for src in srcs:
            url = src if src.startswith(("http://", "https://")) else urljoin(base_url, src)
            try:
                r = c.get(url)
                if r.status_code == 200:
                    wh = _img_size(r.content[:FETCH_BYTES])
                    if wh and wh[0] and wh[1]:
                        sizes[src] = wh
            except Exception:
                continue
    return sizes


def run_image_dims(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        try:
            widget_id, old_html = _find_html_widget(client, page_id)
        except (AbilitiesError, AbilitiesUnavailable) as exc:
            run.status = "failed"
            run.summary = f"Could not read the page: {exc}"
            db.commit()
            return
        if not widget_id or not old_html:
            run.status = "failed"
            run.summary = "No editable HTML widget on this page."
            db.commit()
            return

        srcs = _missing_dims_srcs(old_html)
        if not srcs:
            run.status = "completed"
            run.summary = "No images missing dimensions."
            db.commit()
            return
        sizes = _measure(site.url, srcs)
        if not sizes:
            run.status = "completed"
            run.summary = "Couldn't measure any images (downloads failed)."
            db.commit()
            return
        new_html = _add_dims(old_html, sizes)
        if new_html == old_html:
            run.status = "completed"
            run.summary = "Nothing to change."
            db.commit()
            return

        change = SiteChange(
            site_id=site_id, kind="img_dims",
            request=f"Add dimensions to {len(sizes)} image(s) on {page_title or page_id}",
            css=new_html, old_css=old_html, status="proposed",
            target_page_id=page_id, target_widget_id=widget_id,
        )
        db.add(change)
        db.commit()
        db.refresh(change)
        db.add(Approval(
            site_id=site_id, kind="img_dims",
            title=f"Set dimensions on {len(sizes)} image(s): {page_title or page_id}",
            summary=f"Adds width/height to {len(sizes)} image(s) to stop layout shift (CLS). "
                    "No visual change; one-click revert.",
            # Keep `sizes` so approve can re-apply against the LIVE page (avoids
            # clobbering any other change made to the same widget since proposing).
            payload=__import__("json").dumps({"change_id": change.id, "page_id": page_id,
                                              "widget_id": widget_id, "count": len(sizes),
                                              "sizes": sizes}),
            status="pending",
        ))
        run.status = "completed"
        run.summary = f"Proposed dimensions for {len(sizes)} image(s) — waiting for approval."
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


def start_image_dims_async(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_image_dims, args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
