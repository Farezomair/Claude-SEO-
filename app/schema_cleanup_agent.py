"""Schema-cleanup doer — removes broken, placeholder, or deprecated JSON-LD.

Bad structured data is worse than none (Google can penalize or ignore a page), so
the safe, reliable fix is removal. This finds every
`<script type="application/ld+json">` block in `_meridian_body` and drops the ones
that are invalid JSON, contain placeholder text, or use a deprecated @type
(FAQPage / HowTo — Google retired their rich results), then writes the page back
and verifies the bad block is gone. Per page, auto-applied, reversible. Good
Organization / LocalBusiness schema (from the Schema Agent) is left untouched.
"""
import json
import re
import threading

from .database import SessionLocal
from .elementor_agent import AbilitiesClient, read_body, write_body
from .models import Finding, FixRecord, JobRun, RunLog, Site, SiteChange

LDJSON_RE = re.compile(
    r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
DEPRECATED = {"faqpage", "howto"}
PLACEHOLDER_RE = re.compile(
    r"\[[^\]]{1,40}\]|YOUR_|\bXXX+\b|\bTODO\b|example\.com|placeholder|lorem ipsum", re.I)


def _types(node) -> set:
    out = set()

    def walk(n):
        if isinstance(n, dict):
            t = n.get("@type")
            if isinstance(t, str):
                out.add(t.lower())
            elif isinstance(t, list):
                out.update(x.lower() for x in t if isinstance(x, str))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def _bad_reason(inner: str) -> str | None:
    """Why this JSON-LD block should be removed, or None to keep it."""
    raw = inner.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return "invalid JSON-LD"
    if _types(data) & DEPRECATED:
        return "deprecated schema (FAQPage/HowTo)"
    if PLACEHOLDER_RE.search(raw):
        return "placeholder text in schema"
    return None


def _clean(html: str) -> tuple[str, list]:
    reasons = []

    def repl(m):
        r = _bad_reason(m.group(1))
        if r:
            reasons.append(r)
            return ""
        return m.group(0)

    return LDJSON_RE.sub(repl, html), reasons


def run_schema_cleanup(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
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
            run.summary = "Page body is empty — nothing to clean."
            db.commit()
            return

        new_html, reasons = _clean(old_html)
        if not reasons or new_html == old_html:
            run.status = "completed"
            run.summary = "No broken/placeholder/deprecated schema on this page."
            db.commit()
            return

        ok = write_body(client, page_id, new_html)
        n = len(reasons)
        db.add(SiteChange(
            site_id=site_id, kind="schema_cleanup",
            request=f"Remove {n} bad JSON-LD block(s) on {page_title or page_id}",
            css=new_html, old_css=old_html, status="applied" if ok else "failed",
            target_page_id=page_id, target_widget_id=""))
        if ok:
            for f in db.query(Finding).filter(
                    Finding.site_id == site_id,
                    Finding.category.in_(("schema_invalid", "schema_placeholder", "schema_deprecated")),
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
                f.remark = f"Auto-fixed: removed {n} bad JSON-LD block(s) — {', '.join(sorted(set(reasons)))} (live)."
            db.add(FixRecord(
                site_id=site_id, doer="Schema-cleanup Agent", field="schema_invalid",
                action_taken=f"Removed {n} JSON-LD block(s) on {page_title or page_id}: {', '.join(sorted(set(reasons)))}",
                page_ref=str(page_id), before_value="(bad structured data present)",
                after_value=f"{n} bad block(s) removed", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
        run.status = "completed"
        run.summary = (f"Removed {n} bad JSON-LD block(s) on {page_title or page_id} — live."
                       if ok else "Schema-cleanup write didn't verify.")
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


def start_schema_cleanup_async(site_id: int, run_id: int, conn: dict, page_id: int, page_title: str = "") -> None:
    threading.Thread(target=run_schema_cleanup, args=(site_id, run_id, conn, page_id, page_title), daemon=True).start()
