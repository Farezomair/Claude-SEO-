"""The Business Auditor — a second lens, separate from SEO.

SEO asks "will Google rank this?". This asks "does the website actually serve the
business?" It first learns what the business is (a short setup interview that
reads the site and asks the owner to confirm/fill in the type, revenue model,
goal, audience, and competitors), then scores Business Fitness across model fit,
offer clarity, conversion path, trust/proof, and differentiation — and compares
the site to competitors (named by the owner or proposed by the model from its own
knowledge of the space). Its score never touches the SEO score.
"""
import json
import re

import httpx
from bs4 import BeautifulSoup

from .brain import ANTHROPIC_MODEL, _extract_json, _get_client
from .database import SessionLocal
from .models import BusinessAudit, BusinessProfile, JobRun, RunLog, Site

REQUEST_TIMEOUT = 15.0
UA = "Mozilla/5.0 (Ascend Business Auditor)"


def _grade(score: int) -> str:
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"


def _page_text(url: str, limit: int = 4000) -> str:
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            html = c.get(url).text
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        title = (soup.title.string if soup.title else "") or ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        return f"TITLE: {title}\n{text}"[:limit]
    except Exception:
        return ""


def _site_snapshot(site_url: str) -> str:
    """Homepage + a couple of internal pages, as text, for the model to read."""
    base = site_url.rstrip("/")
    parts = [f"--- HOMEPAGE ---\n{_page_text(base + '/')}"]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            home = c.get(base + "/").text
        soup = BeautifulSoup(home, "html.parser")
        seen, picked = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/") and not href.startswith("//"):
                path = href.split("#")[0].split("?")[0].rstrip("/")
                if path and path not in seen and not re.search(
                        r"privacy|terms|cart|checkout|login|account|wp-", path, re.I):
                    seen.add(path)
                    picked.append(base + path)
            if len(picked) >= 3:
                break
        for u in picked:
            t = _page_text(u, 1800)
            if t:
                parts.append(f"--- {u} ---\n{t}")
    except Exception:
        pass
    return "\n\n".join(parts)[:9000]


def interview(site_url: str, site_name: str) -> dict:
    """Read the site and produce the setup interview: a detected business type and
    tailored questions with suggested answers the owner confirms or edits."""
    snap = _site_snapshot(site_url)
    prompt = f"""You are onboarding a business into a Business Auditor. Read what this website
shows and prepare a SHORT setup interview so the owner can confirm what their business is.

Website: {site_name} ({site_url})

What the site currently shows:
\"\"\"{snap or '(could not read the site)'}\"\"\"

From this, infer the business as best you can, then return fields for the owner to CONFIRM or
correct. For each field give a "suggestion" (your best inference from the site; empty string if
you truly can't tell). Also propose 2-4 likely COMPETITORS from your own knowledge of this kind
of business and market (real companies/sites if you know them; include a url when you're confident).

Respond with ONLY a JSON object:
{{"detected_type": "one short phrase",
  "fields": {{
    "business_type": {{"label": "What kind of business is this?", "suggestion": "..."}},
    "revenue_model": {{"label": "How does it make money?", "suggestion": "..."}},
    "primary_goal": {{"label": "What should this website achieve? (leads, sales, bookings, signups)", "suggestion": "..."}},
    "audience": {{"label": "Who is the target customer?", "suggestion": "..."}},
    "offerings": {{"label": "Main products or services offered", "suggestion": "..."}}
  }},
  "suggested_competitors": [{{"name": "...", "url": "https://..."}}]
}}"""
    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=1500,
        system="You onboard businesses. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json(next((b.text for b in response.content if b.type == "text"), ""))
    fields = data.get("fields") or {}
    comps = []
    for c in (data.get("suggested_competitors") or [])[:6]:
        nm = str(c.get("name", "")).strip()
        if nm:
            comps.append({"name": nm[:120], "url": str(c.get("url", "")).strip()[:300]})
    return {
        "detected_type": str(data.get("detected_type", "")).strip()[:120],
        "fields": {k: {"label": str(v.get("label", k)), "suggestion": str(v.get("suggestion", "")).strip()[:400]}
                   for k, v in fields.items()},
        "suggested_competitors": comps,
    }


def _competitor_snapshots(competitors: list) -> str:
    """Fetch each competitor URL (if given) so the model compares against REAL
    current pages, not just its training memory. Names without URLs are compared
    from the model's own knowledge."""
    out = []
    for c in competitors[:4]:
        name, url = c.get("name", ""), (c.get("url") or "").strip()
        if url and url.startswith(("http://", "https://")):
            t = _page_text(url, 2200)
            out.append(f"--- COMPETITOR: {name or url} ({url}) ---\n{t or '(could not fetch)'}")
        elif name:
            out.append(f"--- COMPETITOR: {name} (no url — compare from your knowledge) ---")
    return "\n\n".join(out)[:7000]


def run_business_audit(site_id: int, run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        prof = db.query(BusinessProfile).filter(BusinessProfile.site_id == site_id).first()
        if not prof:
            run.status = "failed"
            run.summary = "Set up the Business Auditor first (tell it what the business is)."
            db.commit()
            return

        def _label(t):
            run.progress_label = t[:290]
            db.commit()

        _label("Reading the website…")
        snap = _site_snapshot(site.url)
        try:
            competitors = json.loads(prof.competitors_json or "[]")
        except Exception:
            competitors = []
        _label("Analyzing competitors…")
        comp_text = _competitor_snapshots(competitors)

        prompt = f"""You are a Business Auditor (NOT an SEO auditor — ignore SEO/ranking mechanics).
Judge whether this WEBSITE actually serves the BUSINESS and its goals, and how it stacks up
against competitors.

THE BUSINESS (owner-confirmed):
- Name: {site.name} ({site.url})
- Type: {prof.business_type}
- Revenue model: {prof.revenue_model}
- Primary goal of the site: {prof.primary_goal}
- Target customer: {prof.audience}
- Offerings: {prof.offerings}
- Notes: {prof.extra_notes or '(none)'}

WHAT THE SITE CURRENTLY SHOWS:
\"\"\"{snap or '(could not read the site)'}\"\"\"

COMPETITORS:
\"\"\"{comp_text or '(none provided/fetched — use your knowledge of this market)'}\"\"\"

Evaluate against what THIS KIND of business needs to succeed online. Call out concrete gaps like
"this is a dropshipping store but there are no products or cart on the site", or "competitors show
an owner bio and guarantees; this site shows neither". Score 5 categories 0-100:
- model_fit: does the site deliver what this business model requires (e.g. products+cart for a
  store, booking/quote for a service, signup/pricing for SaaS)?
- offer_clarity: is the value proposition and what they sell immediately clear?
- conversion_path: is there a clear path to the primary goal (buy/book/contact/sign up)?
- trust_proof: credible proof for THIS audience (real reviews, portfolio, credentials, guarantees, named team)?
- differentiation: does it stand out vs competitors, or is it generic?

Respond with ONLY a JSON object:
{{"overall": 0-100,
  "summary": "3-5 sentence plain-English read of how the site performs AS A BUSINESS and the #1 thing to fix",
  "categories": [{{"key": "model_fit|offer_clarity|conversion_path|trust_proof|differentiation", "label": "Model fit", "score": 0-100, "note": "one line"}}],
  "findings": [{{"severity": "high|medium|low", "title": "short", "detail": "specific, actionable"}}],
  "competitors": [{{"name": "...", "url": "...", "strengths": "what they do well", "gaps_vs_you": "what they have that this site lacks"}}]}}"""
        _label("Scoring business fitness…")
        response = _get_client().messages.create(
            model=ANTHROPIC_MODEL, max_tokens=2600,
            system="You are a pragmatic business analyst. You respond only with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(next((b.text for b in response.content if b.type == "text"), ""))
        overall = max(0, min(100, int(data.get("overall", 0) or 0)))
        cats = []
        for c in (data.get("categories") or [])[:6]:
            cats.append({"key": str(c.get("key", "")), "label": str(c.get("label", ""))[:40],
                         "score": max(0, min(100, int(c.get("score", 0) or 0))),
                         "note": str(c.get("note", ""))[:200]})
        findings = [{"severity": str(f.get("severity", "medium")), "title": str(f.get("title", ""))[:120],
                     "detail": str(f.get("detail", ""))[:500]}
                    for f in (data.get("findings") or [])[:12]]
        comps = [{"name": str(c.get("name", ""))[:120], "url": str(c.get("url", ""))[:300],
                  "strengths": str(c.get("strengths", ""))[:300], "gaps_vs_you": str(c.get("gaps_vs_you", ""))[:300]}
                 for c in (data.get("competitors") or [])[:6]]

        db.add(BusinessAudit(
            site_id=site_id, score=overall, grade=_grade(overall),
            categories_json=json.dumps(cats), findings_json=json.dumps(findings),
            competitors_json=json.dumps(comps), summary=str(data.get("summary", ""))[:1200]))
        run.status = "completed"
        run.summary = f"Business Fitness: {overall}/100 ({_grade(overall)}). " + str(data.get("summary", ""))[:180]
        db.add(RunLog(site_id=site_id, message=f"Business audit: {overall}/100 ({_grade(overall)})"))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Business audit failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_business_audit_async(site_id: int, run_id: int) -> None:
    import threading
    threading.Thread(target=run_business_audit, args=(site_id, run_id), daemon=True).start()


def latest_audit(db, site_id: int):
    return (db.query(BusinessAudit).filter(BusinessAudit.site_id == site_id)
            .order_by(BusinessAudit.created_at.desc()).first())
