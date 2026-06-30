"""Technical SEO doer — server-level fixes the body/meta doers can't reach.

Grounded in the seo-technical skill (MIT, AgriciDaniel) §3 Security + §1 Crawlability:
- Security response headers (HSTS, X-Content-Type-Options, X-Frame-Options,
  Referrer-Policy, Content-Security-Policy: upgrade-insecure-requests).
- /llms.txt (emerging standard for guiding AI assistants).

Both are applied via the SEO Agent Bridge plugin's /seo-agent/v1/tech endpoint
(PHP `send_headers` + a /llms.txt route) and then INDEPENDENTLY verified by
re-fetching the live site — a finding is only closed if the header / file is
actually present. Auto-applied: site-config, invisible to visitors, reversible
(toggle off). Fixes `security_headers` and `no_llms_txt` findings.
"""
import threading

import httpx

from .abilities import USER_AGENT
from .database import SessionLocal
from .models import Finding, FixRecord, JobRun, RunLog, Site

# The headers the crawler checks for (crawler.py SECURITY_HEADERS keys).
SEC_HEADERS_REQUIRED = ["strict-transport-security", "x-content-type-options",
                        "x-frame-options", "content-security-policy"]


def _base(conn: dict) -> str:
    url = conn["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _tech_post(conn: dict, payload: dict) -> bool:
    """POST to the Bridge /tech endpoint (admin app-password). True on 2xx."""
    try:
        with httpx.Client(timeout=30.0, auth=(conn["username"], conn["app_password"]),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            r = c.post(_base(conn) + "/wp-json/seo-agent/v1/tech", json=payload)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _headers_live(site_url: str) -> bool:
    """Re-fetch the live homepage and confirm all required headers are present."""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(site_url)
        present = {k.lower() for k in r.headers.keys()}
        return all(h in present for h in SEC_HEADERS_REQUIRED)
    except Exception:
        return False


def _llms_live(site_url: str) -> bool:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(site_url.rstrip("/") + "/llms.txt")
        return r.status_code == 200 and len(r.text.strip()) > 20
    except Exception:
        return False


def _llms_txt(site) -> str:
    """A minimal, valid llms.txt (markdown) describing the site + pointing to the
    sitemap. Per the llms.txt convention: H1 name, a blockquote summary, links."""
    base = (site.url or "").rstrip("/")
    name = site.name or base
    return (f"# {name}\n\n"
            f"> {name}. Authoritative content lives on this site; the sitemap below "
            f"lists every page for AI assistants and crawlers.\n\n"
            f"## Key resources\n"
            f"- [Sitemap]({base}/sitemap.xml)\n"
            f"- [Homepage]({base}/)\n")


def _close(db, site_id, categories, remark):
    n = 0
    for f in (db.query(Finding).filter(Finding.site_id == site_id,
                                       Finding.category.in_(categories),
                                       Finding.status.in_(("open", "in-progress"))).all()):
        f.status = "closed"
        f.remark = remark
        n += 1
    return n


def run_technical_fixes(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        done = []

        # 1) Security headers — enable, then verify they're actually live.
        if _tech_post(conn, {"security_headers": True}) and _headers_live(site.url):
            _close(db, site_id, ("security_headers",), "Auto-fixed: security headers now sent (verified live).")
            db.add(FixRecord(
                site_id=site_id, doer="SEO Technical", field="security_headers",
                action_taken="Enabled HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, "
                              "CSP (upgrade-insecure-requests) via send_headers",
                page_ref=site.url, before_value="(missing security headers)",
                after_value="5 security headers sent", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
            done.append("security headers")

        # 2) llms.txt — publish, then verify it serves.
        if _tech_post(conn, {"llms_txt": _llms_txt(site)}) and _llms_live(site.url):
            _close(db, site_id, ("no_llms_txt",), "Auto-fixed: /llms.txt now published (verified live).")
            db.add(FixRecord(
                site_id=site_id, doer="SEO Technical", field="no_llms_txt",
                action_taken="Published /llms.txt (site summary + sitemap link)",
                page_ref=site.url.rstrip("/") + "/llms.txt", before_value="(no llms.txt)",
                after_value="llms.txt served", method="auto-safe", lane="autonomous",
                applied=True, verification_verdict="verified", status="done"))
            done.append("llms.txt")

        run.status = "completed"
        run.summary = ("Technical: applied " + ", ".join(done) + " (verified live)." if done
                       else "Technical: couldn't verify the fixes live — is SEO Agent Bridge (v5+) active?")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Technical run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_technical_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_technical_fixes, args=(site_id, run_id, conn), daemon=True).start()
