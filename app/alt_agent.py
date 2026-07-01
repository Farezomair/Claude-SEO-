"""Alt-text doer — writes descriptive alt text for images missing it.

The pages are single Elementor html widgets (rendered from `_meridian_body`) whose
hand-built `<img>` tags often lack alt text, which hurts accessibility and image
SEO. This finds every image with no (or empty) alt, asks Claude for concise,
specific alt grounded in the filename + nearby visible text, and injects
`alt="..."` via byte-preserving regex (no other markup touched). Auto-applied +
verified live through the same `_meridian_body` write path as the image-dimensions
doer, and idempotent — an image that already has alt is left alone.
"""
import re
import threading

from bs4 import BeautifulSoup

from .brain import generate_alt_texts
from .database import SessionLocal
from .elementor_agent import AbilitiesClient, read_body, write_body
from .models import Finding, FixRecord, JobRun, RunLog, Site, SiteChange

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)
ALT_RE = re.compile(r"""\balt\s*=\s*["']([^"']*)["']""", re.I)
MAX_IMAGES = 12  # bounded so a page's images can't stall the run


def _needs_alt(tag: str) -> bool:
    """True if the <img> has no alt attribute, or an empty/whitespace alt."""
    m = ALT_RE.search(tag)
    return m is None or not m.group(1).strip()


def _imgs_needing_alt(html: str) -> list[tuple[str, str]]:
    """Return [(src, nearby_text)] for images missing alt (deduped by src)."""
    out, seen = [], set()
    for m in IMG_RE.finditer(html):
        tag = m.group(0)
        if not _needs_alt(tag):
            continue
        sm = SRC_RE.search(tag)
        if not sm or sm.group(1) in seen:
            continue
        src = sm.group(1)
        seen.add(src)
        ctx = BeautifulSoup(html[max(0, m.start() - 400):m.end() + 220], "html.parser").get_text(" ", strip=True)
        out.append((src, ctx[:200]))
        if len(out) >= MAX_IMAGES:
            break
    return out


def _apply_alts(html: str, alts: dict) -> tuple[str, int]:
    """Insert alt="..." into each img missing it, matched by src. Idempotent —
    only touches imgs that still need alt AND have a generated value. Returns
    (new_html, count_applied)."""
    applied = [0]

    def repl(m):
        tag = m.group(0)
        if not _needs_alt(tag):
            return tag
        sm = SRC_RE.search(tag)
        if not sm or sm.group(1) not in alts:
            return tag
        alt = alts[sm.group(1)].replace('"', "'")
        applied[0] += 1
        if ALT_RE.search(tag):  # replace an empty alt="" in place
            return ALT_RE.sub(f'alt="{alt}"', tag, count=1)
        return re.sub(r"\s*/?>\s*$", f' alt="{alt}">', tag, count=1)

    return IMG_RE.sub(repl, html), applied[0]


def run_alt_text(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        old_html = read_body(client, page_id)
        if old_html is None:
            run.status = "failed"
            run.summary = "Couldn't read the page body — is SEO Agent Bridge (v4+) active?"
            db.commit()
            return
        if not old_html:
            run.status = "completed"
            run.summary = "Page body is empty — nothing to describe."
            db.commit()
            return

        need = _imgs_needing_alt(old_html)
        if not need:
            run.status = "completed"
            run.summary = "No images missing alt text."
            db.commit()
            return
        try:
            alts = generate_alt_texts(site.name, page_title or str(page_id),
                                      [{"src": s, "context": c} for s, c in need])
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Alt-text generation failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return
        alts = {k: v.strip() for k, v in (alts or {}).items() if v and v.strip()}
        if not alts:
            run.status = "completed"
            run.summary = "No alt text produced (images may be decorative)."
            db.commit()
            return

        new_html, n = _apply_alts(old_html, alts)
        if n == 0 or new_html == old_html:
            run.status = "completed"
            run.summary = "Nothing to change."
            db.commit()
            return

        # AUTO-APPLY: alt text is invisible to sighted visitors and one-click
        # revertible, so it writes straight to the live body (like image dims).
        ok = write_body(client, page_id, new_html)
        db.add(SiteChange(
            site_id=site_id, kind="alt_text",
            request=f"Add alt text to {n} image(s) on {page_title or page_id}",
            css=new_html, old_css=old_html, status="applied" if ok else "failed",
            target_page_id=page_id, target_widget_id=""))
        if ok:
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id, Finding.category == "images_missing_alt",
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = f"Auto-fixed: alt text added to {n} image(s) (live)."
            db.add(FixRecord(
                site_id=site_id, doer="Alt-text Agent", field="images_missing_alt",
                action_taken=f"Auto-wrote alt text for {n} image(s) on {page_title or page_id} (via _meridian_body)",
                page_ref=str(page_id), before_value="(images missing alt text)",
                after_value=f"{n} images described", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
        run.status = "completed"
        run.summary = (f"Auto-added alt text to {n} image(s) on {page_title or page_id} — live."
                       if ok else "Alt-text write didn't verify.")
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


def start_alt_text_async(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_alt_text, args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
