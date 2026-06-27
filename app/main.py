"""SEO Agent System — Stage 1: the empty shell.

A web app with a single-owner login, a Websites list, an Add-website button,
and a Postgres-backed workspace per site. No agents yet — this stage exists to
prove we can build, deploy, and store isolated per-site data.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import check_credentials, current_user
from .connections import get_connection
from . import google_oauth
from .content_agent import start_draft_async
from .content_corrector import start_correction_async
from .content_standard import scan
from .crypto import encrypt
from .database import Base, engine, get_db
from .jobs import start_audit_async
from .migrations import ensure_columns
from .models import (
    Approval, Audit, Content, Finding, FixRecord, JobRun, Report, RunLog, Site,
    SiteChange, SiteConnection, utcnow,
)
from .rules import SCOPES as RULE_SCOPES
from .rules import get_rules, set_rules
from .scheduler import ENABLED as SCHEDULER_ENABLED
from .scheduler import INTERVAL_DAYS, is_due, start_scheduler
from .onpage_agent import start_meta_rewrites_async
from .seo_technical import start_dedupe_async, start_metafix_async
from .website_agent import start_change_async, start_page_drafts_async
from .elementor_agent import (
    apply_html, copy_diff, list_elementor_pages, start_page_rewrite_async, verify_html,
)
from .weekly import start_weekly_async
from .wordpress import YOAST_DESC_KEY, YOAST_TITLE_KEY, WordPressClient, WordPressError
from .abilities import AbilitiesClient, AbilitiesError, AbilitiesUnavailable

# Whether approved content is sent to WordPress as a draft (safe — needs a final
# Publish click in WP) or published live. Defaults to draft.
CONTENT_PUBLISH_STATUS = os.getenv("CONTENT_PUBLISH_STATUS", "draft")

# Create tables on startup, then add any columns missing from existing tables.
Base.metadata.create_all(bind=engine)
ensure_columns(engine)

# Start the weekly scheduler (set WEEKLY_ENABLED=false to disable).
start_scheduler()

app = FastAPI(title="SEO Agent System")

# Session cookie signing key. MUST be set in production via env var; the dev
# fallback only keeps local runs working.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-only-insecure-change-me"),
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Bumped on each deploy so we can confirm which build is live (public, no auth).
BUILD = "elementor-polish-6"


@app.get("/version")
def version():
    return JSONResponse({"build": BUILD})

# Map a status string to a colored badge.
_BADGE_CLASS = {
    "verified": "success", "completed": "success", "published": "success",
    "approved": "success", "connected": "success", "applied": "success",
    "running": "warning", "pending": "warning", "proposed": "warning",
    "drafting": "warning", "in wordpress draft": "info", "draft": "neutral",
    "failed": "danger", "rejected": "danger", "reverted": "neutral",
    "high": "danger", "medium": "warning", "low": "info",
    # Finding / verification statuses
    "blocker": "danger", "critical": "danger",
    "open": "warning", "reopened": "warning", "escalated": "danger", "closed": "success",
    "verified": "success", "not fixed": "danger", "partial": "warning",
    "regressed": "danger", "handed off": "neutral", "needs human input": "warning",
    # action classes
    "auto-safe": "success", "auto safe": "success",
    "needs-approval": "warning", "needs approval": "warning",
    "needs-human": "info", "needs human": "info",
}


def _badge(status) -> Markup:
    s = (str(status) or "").replace("_", " ").strip()
    cls = _BADGE_CLASS.get(s.lower(), "neutral")
    return Markup(f'<span class="badge badge-{cls}">{s}</span>')


templates.env.filters["badge"] = _badge


# --------------------------------------------------------------------------
# Health check (Railway pings this)
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/sites", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if check_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse("/sites", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Wrong username or password."},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------
@app.get("/")
def root(request: Request):
    return RedirectResponse("/sites" if current_user(request) else "/login", status_code=303)


@app.get("/sites", response_class=HTMLResponse)
def list_sites(request: Request, gnotice: str = "", db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    sites = db.query(Site).order_by(Site.created_at.desc()).all()
    pending_count = db.query(Approval).filter(Approval.status == "pending").count()
    return templates.TemplateResponse(
        "sites.html",
        {"request": request, "sites": sites, "user": current_user(request),
         "pending_count": pending_count, "gnotice": gnotice},
    )


@app.post("/sites")
def add_site(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    name = name.strip()
    url = url.strip()
    if name and url:
        db.add(Site(name=name, url=url))
        db.commit()
    return RedirectResponse("/sites", status_code=303)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(
    site_id: int,
    request: Request,
    tab: str = "audit",
    notice: str = "",
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    if tab not in {"audit", "fixes", "content", "website", "reports", "settings"}:
        tab = "audit"

    ctx = {
        "request": request,
        "site": site,
        "tab": tab,
        "user": current_user(request),
        "notice": notice,
        "latest_audit": None,
        "findings": [],
        "latest_fix_run": None,
        "fix_records": [],
        "connection": None,
        "connection_active": False,
        "latest_draft_run": None,
        "drafts": [],
        "reports": [],
        "changes": [],
        "latest_change_run": None,
        "latest_weekly_run": None,
        "scheduler_enabled": SCHEDULER_ENABLED,
        "interval_days": INTERVAL_DAYS,
        "weekly_due": is_due(db, site_id),
        "pending_count": db.query(Approval).filter(Approval.status == "pending").count(),
    }

    if tab == "audit":
        latest_audit = (
            db.query(Audit).filter(Audit.site_id == site_id)
            .order_by(Audit.created_at.desc()).first()
        )
        ctx["latest_audit"] = latest_audit
        if latest_audit and latest_audit.status == "completed":
            severity_rank = {"blocker": 0, "critical": 1, "high": 2, "medium": 3, "low": 4}
            findings = db.query(Finding).filter(Finding.audit_id == latest_audit.id).all()
            findings.sort(key=lambda f: (severity_rank.get(f.severity, 5), f.category))
            ctx["findings"] = findings
            # Phase C: how many missing-page findings the Website Agent can draft.
            ctx["draftable_pages"] = sum(1 for f in findings if f.category == "required_page_missing")
            ctx["page_draft_running"] = (
                db.query(JobRun).filter(
                    JobRun.site_id == site_id, JobRun.kind == "pagedraft", JobRun.status == "running"
                ).first() is not None
            )
            ctx["page_connection_active"] = get_connection(site_id, site.url, site.name) is not None
            # How many Search Console ranking opportunities SEO On-page can act on.
            ctx["ranking_opps"] = sum(
                1 for f in findings if f.category in ("striking_distance", "low_ctr")
            )
            ctx["onpage_running"] = (
                db.query(JobRun).filter(
                    JobRun.site_id == site_id, JobRun.kind == "onpage", JobRun.status == "running"
                ).first() is not None
            )
            # Duplicate-title findings SEO Technical can make unique.
            ctx["dup_titles"] = sum(1 for f in findings if f.category == "duplicate_title")
            ctx["dedupe_running"] = (
                db.query(JobRun).filter(
                    JobRun.site_id == site_id, JobRun.kind == "dedupe", JobRun.status == "running"
                ).first() is not None
            )

    elif tab == "fixes":
        ctx["latest_fix_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "metafix")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["fix_records"] = (
            db.query(FixRecord).filter(FixRecord.site_id == site_id)
            .order_by(FixRecord.created_at.desc()).limit(50).all()
        )
        ctx["connection_active"] = get_connection(site_id, site.url, site.name) is not None

    elif tab == "content":
        ctx["latest_draft_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "content_draft")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["latest_correction_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "contentfix")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["content_connection_active"] = get_connection(site_id, site.url, site.name) is not None
        ctx["drafts"] = (
            db.query(Content).filter(Content.site_id == site_id)
            .order_by(Content.created_at.desc()).limit(30).all()
        )

    elif tab == "website":
        ctx["latest_change_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "website")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["changes"] = (
            db.query(SiteChange).filter(SiteChange.site_id == site_id)
            .order_by(SiteChange.created_at.desc()).limit(30).all()
        )
        conn_w = get_connection(site_id, site.url, site.name)
        ctx["connection_active"] = conn_w is not None
        ctx["elementor_pages"] = list_elementor_pages(conn_w) if conn_w else []
        latest_el = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "elementor")
            .order_by(JobRun.created_at.desc()).first()
        )
        # Stale-job guard: a rewrite that's been "running" too long means the worker
        # was lost (e.g. a server recycle). Mark it failed so the UI unblocks and the
        # owner can retry, instead of refreshing on "Rewriting…" forever.
        if (latest_el and latest_el.status == "running" and latest_el.created_at
                and (utcnow() - latest_el.created_at) > timedelta(minutes=5)):
            latest_el.status = "failed"
            latest_el.summary = "Timed out (the rewrite worker was interrupted). Please try again."
            db.add(RunLog(site_id=site_id, message=f"Elementor run #{latest_el.id} marked stale after 5 min."))
            db.commit()
        ctx["latest_elementor_run"] = latest_el
        ctx["elementor_running"] = bool(latest_el and latest_el.status == "running")

    elif tab == "reports":
        ctx["latest_weekly_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "weekly")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["reports"] = (
            db.query(Report).filter(Report.site_id == site_id)
            .order_by(Report.created_at.desc()).limit(20).all()
        )

    elif tab == "settings":
        ctx["connection"] = (
            db.query(SiteConnection).filter(SiteConnection.site_id == site_id).first()
        )
        ctx["connection_active"] = get_connection(site_id, site.url, site.name) is not None
        ctx["google"] = google_oauth.connection()
        ctx["google_configured"] = google_oauth.configured()

    return templates.TemplateResponse("site_detail.html", ctx)


@app.post("/sites/{site_id}/run-weekly")
def run_weekly_now(site_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)

    already_running = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "weekly", JobRun.status == "running")
        .first()
    )
    if not already_running:
        run = JobRun(site_id=site_id, kind="weekly", status="running", summary="Weekly run starting…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_weekly_async(site_id, run.id)

    return RedirectResponse(f"/sites/{site_id}?tab=reports", status_code=303)


@app.post("/sites/{site_id}/request-change")
def request_change(
    site_id: int,
    request: Request,
    change: str = Form(...),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)

    if change.strip():
        already = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "website", JobRun.status == "running")
            .first()
        )
        if not already:
            run = JobRun(site_id=site_id, kind="website", status="running", summary="Designing change…")
            db.add(run)
            db.commit()
            db.refresh(run)
            start_change_async(site_id, run.id, change.strip())
    return RedirectResponse(f"/sites/{site_id}?tab=website", status_code=303)


@app.post("/sites/{site_id}/changes/{change_id}/revert")
def revert_change(site_id: int, change_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    change = db.get(SiteChange, change_id)
    if not site or not change or change.site_id != site_id or change.status != "applied":
        return RedirectResponse(f"/sites/{site_id}?tab=website", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if conn:
        try:
            if change.kind == "page_rewrite":
                client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
                apply_html(client, change.target_page_id, change.target_widget_id, change.old_css)
            else:
                WordPressClient(conn["url"], conn["username"], conn["app_password"]).update_custom_css(change.old_css)
            change.status = "reverted"
            db.add(RunLog(site_id=site_id, message=f"Reverted change: {change.request[:80]}"))
            db.commit()
        except (WordPressError, AbilitiesError, AbilitiesUnavailable):
            return RedirectResponse(f"/sites/{site_id}?tab=website&notice=revert_fail", status_code=303)
    return RedirectResponse(f"/sites/{site_id}?tab=website&notice=reverted", status_code=303)


def _google_redirect_uri(request: Request) -> str:
    env = os.getenv("GOOGLE_REDIRECT_URI")
    if env:
        return env
    uri = str(request.url_for("google_callback"))
    # Railway terminates TLS, so the internal request looks like http. Force https
    # for the public redirect (must match what's registered on the OAuth client).
    if uri.startswith("http://") and "localhost" not in uri and "127.0.0.1" not in uri:
        uri = "https://" + uri[len("http://"):]
    return uri


@app.get("/google/connect")
def google_connect(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    if not google_oauth.configured():
        return RedirectResponse("/sites?gnotice=not_configured", status_code=303)
    return RedirectResponse(google_oauth.auth_url(_google_redirect_uri(request)))


@app.get("/google/callback", name="google_callback")
def google_callback(request: Request, code: str = "", error: str = ""):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    if error or not code:
        return RedirectResponse("/sites?gnotice=denied", status_code=303)
    try:
        refresh, email = google_oauth.exchange_code(code, _google_redirect_uri(request))
        if not refresh:
            return RedirectResponse("/sites?gnotice=no_refresh", status_code=303)
        google_oauth.save_connection(refresh, email)
    except Exception:
        return RedirectResponse("/sites?gnotice=failed", status_code=303)
    return RedirectResponse("/sites?gnotice=connected", status_code=303)


@app.post("/google/disconnect")
def google_disconnect(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    google_oauth.disconnect()
    return RedirectResponse("/sites?gnotice=disconnected", status_code=303)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, notice: str = "", db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    rules = [{"scope": s, "label": label, "content": get_rules(s)} for s, label in RULE_SCOPES]
    pending_count = db.query(Approval).filter(Approval.status == "pending").count()
    return templates.TemplateResponse(
        "rules.html",
        {"request": request, "user": current_user(request), "rules": rules,
         "notice": notice, "pending_count": pending_count},
    )


@app.post("/rules")
async def save_rules(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    for scope, _label in RULE_SCOPES:
        set_rules(scope, (form.get(scope) or "").strip())
    return RedirectResponse("/rules?notice=saved", status_code=303)


@app.post("/sites/{site_id}/draft-content")
def draft_content(
    site_id: int,
    request: Request,
    topic: str = Form(""),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)

    already_running = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "content_draft", JobRun.status == "running")
        .first()
    )
    if not already_running:
        run = JobRun(site_id=site_id, kind="content_draft", status="running", summary="Drafting…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_draft_async(site_id, run.id, topic.strip())

    return RedirectResponse(f"/sites/{site_id}?tab=content", status_code=303)


@app.post("/sites/{site_id}/correct-content")
def correct_content_route(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Content Corrector: scan blog posts and draft writing-standard cleanups."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)
    already = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "contentfix", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="contentfix", status="running", summary="Scanning content…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_correction_async(site_id, run.id, conn)
    return RedirectResponse(f"/sites/{site_id}?tab=content", status_code=303)


@app.post("/sites/{site_id}/fix-duplicate-titles")
def fix_duplicate_titles(site_id: int, request: Request, db: Session = Depends(get_db)):
    """SEO Technical: make duplicate page titles unique."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)
    already = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "dedupe", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="dedupe", status="running", summary="De-duplicating titles…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_dedupe_async(site_id, run.id, conn)
    return RedirectResponse(f"/sites/{site_id}?tab=audit", status_code=303)


@app.post("/sites/{site_id}/improve-rankings")
def improve_rankings(site_id: int, request: Request, db: Session = Depends(get_db)):
    """SEO On-page: rewrite title/meta for the Search Console ranking opportunities."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)
    already = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "onpage", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="onpage", status="running", summary="Improving ranking pages…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_meta_rewrites_async(site_id, run.id, conn)
    return RedirectResponse(f"/sites/{site_id}?tab=audit", status_code=303)


@app.post("/sites/{site_id}/draft-pages")
def draft_pages(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Website Agent: draft the missing required pages the auditor flagged."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    already = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "pagedraft", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="pagedraft", status="running", summary="Drafting missing pages…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_page_drafts_async(site_id, run.id)
    return RedirectResponse(f"/sites/{site_id}?tab=audit", status_code=303)


@app.get("/sites/{site_id}/abilities")
def discover_abilities(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Build-time discovery: list the site's registered Abilities API catalog.

    Ascend introspects the live WordPress site with its own stored Application
    Password (no human in the loop) so we can see exactly what the headless
    execution layer can do, then design doers against real abilities.
    """
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return JSONResponse({"error": "site not found"}, status_code=404)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return JSONResponse(
            {"error": "No WordPress connection — set it up in Settings first."},
            status_code=400,
        )
    client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
    try:
        abilities = client.list_abilities()
    except AbilitiesUnavailable as exc:
        return JSONResponse(
            {"available": False, "reason": str(exc),
             "hint": "The site does not expose wp-abilities/v1 to this user."},
            status_code=200,
        )
    except AbilitiesError as exc:
        return JSONResponse({"available": True, "error": str(exc)}, status_code=200)

    catalog = []
    for a in abilities:
        catalog.append({
            "name": a.get("name"),
            "label": a.get("label"),
            "description": a.get("description"),
            "category": a.get("category"),
            "input_schema": a.get("input_schema"),
        })
    catalog.sort(key=lambda x: (x.get("category") or "", x.get("name") or ""))
    return JSONResponse({
        "available": True,
        "site": site.url,
        "count": len(catalog),
        "abilities": catalog,
    })


@app.post("/sites/{site_id}/rewrite-page")
def rewrite_page(site_id: int, request: Request, page_id: int = Form(...),
                 page_title: str = Form(""), db: Session = Depends(get_db)):
    """Elementor On-page: full-page SEO rewrite of one Elementor page (gated)."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)
    already = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "elementor", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="elementor", status="running",
                     summary=f"Rewriting “{page_title or page_id}”…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_page_rewrite_async(site_id, run.id, conn, page_id, page_title)
    return RedirectResponse(f"/sites/{site_id}?tab=website", status_code=303)


@app.get("/sites/{site_id}/elementor-probe")
def elementor_probe(site_id: int, request: Request, page_id: int = 0,
                    db: Session = Depends(get_db)):
    """Build-time, READ-ONLY probe of the Elementor abilities' real JSON shapes.

    Returns the raw responses for list-pages and, for the first (or given) page,
    its structure + heading/text widgets — so the editor doer can be built
    against the actual envelope instead of guessed shapes. Changes nothing.
    """
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return JSONResponse({"error": "site not found"}, status_code=404)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return JSONResponse({"error": "No WordPress connection."}, status_code=400)
    client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
    P = "hostinger-ai-assistant"
    WIDGET_TYPES = ["heading", "text-editor", "button", "html", "image", "icon-box", "icon-list"]
    out: dict = {}
    try:
        out["list_pages"] = client.read(f"{P}/elementor-list-pages",
                                        {"post_type": "page", "post_status": "publish", "limit": 30})
    except (AbilitiesError, AbilitiesUnavailable) as exc:
        return JSONResponse({"error": f"list-pages failed: {exc}"}, status_code=200)

    def inspect(pid: int) -> dict:
        res: dict = {}
        for label, name, payload in (
            ("structure", f"{P}/elementor-get-page-structure",
             {"post_id": pid, "include_settings": True}),
            ("find_widgets", f"{P}/elementor-find-widgets",
             {"post_id": pid, "widget_types": WIDGET_TYPES, "include_settings": True}),
        ):
            try:
                res[label] = client.read(name, payload)
            except (AbilitiesError, AbilitiesUnavailable) as exc:
                res[label] = {"error": str(exc)}
        return res

    # Sample representative page types: a service page, a location page, the home
    # page — unless a specific page_id was requested.
    sample_ids = [page_id] if page_id else [22, 39, 12]
    out["inspected"] = {str(pid): inspect(pid) for pid in sample_ids}
    return JSONResponse(out)


# --------------------------------------------------------------------------
# Approvals (the safety gate)
# --------------------------------------------------------------------------
@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, notice: str = "", db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    pending = (
        db.query(Approval, Site)
        .join(Site, Approval.site_id == Site.id)
        .filter(Approval.status == "pending")
        .order_by(Approval.created_at.desc())
        .all()
    )
    items = []
    for appr, site in pending:
        body, code, preview_html, text_diff = "", "", "", []
        try:
            payload = json.loads(appr.payload or "{}")
        except Exception:
            payload = {}
        if appr.kind in ("content", "required_page", "content_fix"):
            content = db.get(Content, payload.get("content_id")) if payload.get("content_id") else None
            body = content.body if content else ""
        elif appr.kind == "website_css":
            change = db.get(SiteChange, payload.get("change_id")) if payload.get("change_id") else None
            code = change.css if change else ""
        elif appr.kind == "page_rewrite":
            change = db.get(SiteChange, payload.get("change_id")) if payload.get("change_id") else None
            if change:
                preview_html = change.css
                text_diff = copy_diff(change.old_css, change.css)
        items.append({"approval": appr, "site": site, "body": body, "code": code,
                      "preview_html": preview_html, "text_diff": text_diff})
    return templates.TemplateResponse(
        "approvals.html",
        {"request": request, "user": current_user(request), "items": items, "notice": notice,
         "pending_count": len(items), "publish_status": CONTENT_PUBLISH_STATUS},
    )


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    appr = db.get(Approval, approval_id)
    if not appr or appr.status != "pending":
        return RedirectResponse("/approvals", status_code=303)

    site = db.get(Site, appr.site_id)
    if appr.kind == "content":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_post(
                content.title, content.body, status=CONTENT_PUBLISH_STATUS, excerpt=appr.summary
            )
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Publish failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        content.status = "published" if CONTENT_PUBLISH_STATUS == "publish" else "in_wordpress_draft"
        db.add(RunLog(
            site_id=site.id,
            message=f"Approved & sent to WordPress ({result.get('status')}): {content.title} {result.get('link', '')}",
        ))

    elif appr.kind == "meta_rewrite":
        payload = json.loads(appr.payload or "{}")
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        kind, page_id = payload.get("page_kind", "posts"), payload.get("page_id")
        new_title, new_desc = payload.get("new_title", ""), payload.get("new_desc", "")
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            meta = {}
            if new_title:
                meta[YOAST_TITLE_KEY] = new_title
            if new_desc:
                meta[YOAST_DESC_KEY] = new_desc
            wp.update_meta(kind, page_id, meta)
            live = wp.get_meta(kind, page_id)
            verified = (not new_title or (live.get(YOAST_TITLE_KEY) or "").strip() == new_title.strip())
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Meta rewrite failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        finding = db.get(Finding, payload.get("finding_id"))
        if finding:
            finding.status = "closed"
        db.add(FixRecord(
            site_id=site.id, finding_id=payload.get("finding_id"), doer="SEO On-page",
            action_taken=f"Rewrote title/description: {new_title}", page_ref=str(page_id),
            field="title+description", before_value=f"{payload.get('old_title', '')} | {payload.get('old_desc', '')}",
            after_value=f"{new_title} | {new_desc}", method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
            outcome_pending=True,  # ranking result lags
        ))
        db.add(RunLog(site_id=site.id, message=f"Applied ranking rewrite: {new_title}"))

    elif appr.kind == "content_fix":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not content:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        kind, page_id = payload.get("page_kind", "posts"), payload.get("page_id")
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            # Snapshot current content for rollback, then update.
            before = ""
            for it in wp.list_content(kinds=(kind,), limit=60):
                if it["id"] == page_id:
                    before = it["content_html"]
                    break
            wp.update_content(kind, page_id, content.body)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Content cleanup failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        after_clean = scan(content.body)
        db.add(FixRecord(
            site_id=site.id, doer="Content Corrector",
            action_taken=f"Editorial cleanup of {content.title}",
            page_ref=str(page_id), before_value=before[:5000], after_value=content.body[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if not after_clean["banned"] and after_clean["em_dashes"] == 0 else "partial",
            status="done",
        ))
        content.status = "published"
        db.add(RunLog(site_id=site.id, message=f"Applied content cleanup: {content.title}"))

    elif appr.kind == "required_page":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not content:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_page(content.title, content.body, status="draft")
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Page create failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        content.status = "in_wordpress_draft"
        finding = db.get(Finding, payload.get("finding_id"))
        if finding:
            finding.status = "closed"
        db.add(RunLog(site_id=site.id, message=f"Created page (draft) in WordPress: {content.title} {result.get('link', '')}"))

    elif appr.kind == "website_css":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            change.old_css = wp.get_custom_css()  # back up the actual current CSS now
            wp.update_custom_css(change.css)
        except WordPressError as exc:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Website change failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        change.status = "applied"
        db.add(RunLog(site_id=site.id, message=f"Applied website change: {change.request[:80]}"))

    elif appr.kind == "page_rewrite":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse("/approvals?notice=no_connection", status_code=303)
        page_id = change.target_page_id or payload.get("page_id")
        widget_id = change.target_widget_id or payload.get("widget_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        try:
            method = apply_html(client, page_id, widget_id, change.css)
        except (AbilitiesError, AbilitiesUnavailable) as exc:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Page rewrite failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse("/approvals?notice=publish_fail", status_code=303)
        verified = verify_html(client, page_id, change.css)
        change.status = "applied"
        db.add(FixRecord(
            site_id=site.id, doer="Elementor On-page",
            action_taken=f"Full-page SEO rewrite of “{appr.title}” (via {method})",
            page_ref=str(page_id), field="page_html",
            before_value=(change.old_css or "")[:5000], after_value=(change.css or "")[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed",
            status="done", outcome_pending=True,
        ))
        db.add(RunLog(
            site_id=site.id,
            message=f"Applied SEO page rewrite: {appr.title} ({'verified live' if verified else 'apply ok, verify pending'})",
        ))

    appr.status = "approved"
    appr.decided_at = utcnow()
    db.commit()
    return RedirectResponse("/approvals?notice=approved", status_code=303)


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    appr = db.get(Approval, approval_id)
    if not appr or appr.status != "pending":
        return RedirectResponse("/approvals", status_code=303)
    try:
        payload = json.loads(appr.payload or "{}")
    except Exception:
        payload = {}
    if appr.kind in ("content", "required_page", "content_fix"):
        content = db.get(Content, payload.get("content_id")) if payload.get("content_id") else None
        if content:
            content.status = "rejected"
        if appr.kind == "required_page":
            finding = db.get(Finding, payload.get("finding_id")) if payload.get("finding_id") else None
            if finding:
                finding.status = "open"  # back to the queue
    elif appr.kind == "meta_rewrite":
        finding = db.get(Finding, payload.get("finding_id")) if payload.get("finding_id") else None
        if finding:
            finding.status = "open"
    elif appr.kind in ("website_css", "page_rewrite"):
        change = db.get(SiteChange, payload.get("change_id")) if payload.get("change_id") else None
        if change:
            change.status = "rejected"
    appr.status = "rejected"
    appr.decided_at = utcnow()
    db.commit()
    return RedirectResponse("/approvals?notice=rejected", status_code=303)


@app.post("/sites/{site_id}/connection")
def save_connection(
    site_id: int,
    request: Request,
    wp_url: str = Form(...),
    wp_username: str = Form(...),
    wp_app_password: str = Form(""),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    if not db.query(Site).filter(Site.id == site_id).first():
        return RedirectResponse("/sites", status_code=303)

    conn = db.query(SiteConnection).filter(SiteConnection.site_id == site_id).first()
    if not conn:
        conn = SiteConnection(site_id=site_id)
        db.add(conn)
    conn.wp_url = wp_url.strip()
    conn.wp_username = wp_username.strip()
    # Only overwrite the password when a new one is entered (it's write-only).
    if wp_app_password.strip():
        conn.wp_app_password_enc = encrypt(wp_app_password.strip())
    db.commit()
    return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=saved", status_code=303)


@app.post("/sites/{site_id}/test-connection")
def test_connection(site_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)
    ok, code = WordPressClient(conn["url"], conn["username"], conn["app_password"]).test()
    notice = "test_ok" if ok else f"test_fail_{code}"
    return RedirectResponse(f"/sites/{site_id}?tab=settings&notice={notice}", status_code=303)


@app.post("/sites/{site_id}/fix-metas")
def run_fix_metas(site_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)

    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return RedirectResponse(f"/sites/{site_id}?tab=settings&notice=test_none", status_code=303)

    already_running = (
        db.query(JobRun)
        .filter(JobRun.site_id == site_id, JobRun.kind == "metafix", JobRun.status == "running")
        .first()
    )
    if not already_running:
        run = JobRun(site_id=site_id, kind="metafix", status="running", summary="Run in progress…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_metafix_async(site_id, run.id, conn)

    return RedirectResponse(f"/sites/{site_id}?tab=fixes", status_code=303)


@app.post("/sites/{site_id}/audit")
def run_site_audit(site_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)

    # Don't start a second audit while one is already running for this site.
    already_running = (
        db.query(Audit)
        .filter(Audit.site_id == site_id, Audit.status == "running")
        .first()
    )
    if not already_running:
        audit = Audit(site_id=site_id, status="running", summary="Audit in progress…")
        db.add(audit)
        db.commit()
        db.refresh(audit)
        start_audit_async(site_id, audit.id, site.url)

    return RedirectResponse(f"/sites/{site_id}?tab=audit", status_code=303)
