"""Performance doer — native lazy-loading for offscreen images.

A safe, no-JS Core Web Vitals win: add loading="lazy" + decoding="async" to every
`<img>` except the first one on the page (kept eager, since the hero image is
usually the LCP element and lazy-loading it would HURT LCP). Real reduction in
initial payload, byte-preserving regex, idempotent (imgs that already declare
loading are left alone).

Honesty note: Core Web Vitals FIELD data (CrUX) is a ~28-day rolling average, so
this APPLIES + records the improvement but does NOT claim an instant CWV change —
the `cwv_poor` finding clears when the audit re-measures. Reversible.
"""
import re
import threading

from .database import SessionLocal
from .elementor_agent import AbilitiesClient, read_body, write_body
from .models import Finding, FixRecord, JobRun, RunLog, Site, SiteChange

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
HAS_LOADING = re.compile(r"\bloading\s*=", re.I)
HAS_DECODING = re.compile(r"\bdecoding\s*=", re.I)


def _lazyload(html: str) -> tuple[str, int]:
    """Add loading=lazy + decoding=async to every <img> after the first. Returns
    (new_html, count_changed)."""
    changed = [0]
    seen = [0]

    def repl(m):
        tag = m.group(0)
        seen[0] += 1
        if seen[0] == 1:  # first image = likely LCP hero -> keep eager
            return tag
        add = ""
        if not HAS_LOADING.search(tag):
            add += ' loading="lazy"'
        if not HAS_DECODING.search(tag):
            add += ' decoding="async"'
        if not add:
            return tag
        changed[0] += 1
        return re.sub(r"\s*/?>\s*$", add + ">", tag, count=1)

    return IMG_RE.sub(repl, html), changed[0]


def run_perf(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
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
            run.summary = "Page body is empty — nothing to optimize."
            db.commit()
            return

        new_html, n = _lazyload(old_html)
        if n == 0 or new_html == old_html:
            run.status = "completed"
            run.summary = "Images already lazy-loaded — nothing to change."
            db.commit()
            return

        ok = write_body(client, page_id, new_html)
        db.add(SiteChange(
            site_id=site_id, kind="perf_lazyload",
            request=f"Lazy-load {n} offscreen image(s) on {page_title or page_id}",
            css=new_html, old_css=old_html, status="applied" if ok else "failed",
            target_page_id=page_id, target_widget_id=""))
        if ok:
            # Real improvement, but CWV field data lags ~28 days — record it, don't
            # claim the finding is verified-fixed. The next measurement reflects it.
            db.add(FixRecord(
                site_id=site_id, doer="Performance Agent", field="cwv_poor",
                action_taken=f"Lazy-loaded {n} offscreen image(s) on {page_title or page_id} (hero kept eager for LCP)",
                page_ref=str(page_id), before_value="(images eager-loaded)",
                after_value=f"{n} images lazy-loaded", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="applied", status="done"))
        run.status = "completed"
        run.summary = (f"Lazy-loaded {n} offscreen image(s) on {page_title or page_id} — live. "
                       "Core Web Vitals field data reflects this over ~4 weeks."
                       if ok else "Lazy-load write didn't verify.")
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


def start_perf_async(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_perf, args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
