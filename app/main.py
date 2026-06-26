"""SEO Agent System — Stage 1: the empty shell.

A web app with a single-owner login, a Websites list, an Add-website button,
and a Postgres-backed workspace per site. No agents yet — this stage exists to
prove we can build, deploy, and store isolated per-site data.
"""
import json
import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import check_credentials, current_user
from .connections import get_connection
from .content_agent import start_draft_async
from .crypto import encrypt
from .database import Base, engine, get_db
from .jobs import start_audit_async
from .migrations import ensure_columns
from .models import (
    Approval, Audit, AuditIssue, Content, Fix, JobRun, Report, RunLog, Site, SiteConnection, utcnow,
)
from .rules import SCOPES as RULE_SCOPES
from .rules import get_rules, set_rules
from .scheduler import ENABLED as SCHEDULER_ENABLED
from .scheduler import INTERVAL_DAYS, is_due, start_scheduler
from .seo_technical import start_metafix_async
from .weekly import start_weekly_async
from .wordpress import WordPressClient, WordPressError

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
def list_sites(request: Request, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    sites = db.query(Site).order_by(Site.created_at.desc()).all()
    pending_count = db.query(Approval).filter(Approval.status == "pending").count()
    return templates.TemplateResponse(
        "sites.html",
        {"request": request, "sites": sites, "user": current_user(request),
         "pending_count": pending_count},
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
    if tab not in {"audit", "fixes", "content", "reports", "settings"}:
        tab = "audit"

    ctx = {
        "request": request,
        "site": site,
        "tab": tab,
        "user": current_user(request),
        "notice": notice,
        "latest_audit": None,
        "issues": [],
        "latest_fix_run": None,
        "fixes": [],
        "connection": None,
        "connection_active": False,
        "latest_draft_run": None,
        "drafts": [],
        "reports": [],
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
            severity_rank = {"high": 0, "medium": 1, "low": 2}
            issues = db.query(AuditIssue).filter(AuditIssue.audit_id == latest_audit.id).all()
            issues.sort(key=lambda i: (severity_rank.get(i.severity, 3), i.category))
            ctx["issues"] = issues

    elif tab == "fixes":
        ctx["latest_fix_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "metafix")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["fixes"] = (
            db.query(Fix).filter(Fix.site_id == site_id)
            .order_by(Fix.created_at.desc()).limit(50).all()
        )
        ctx["connection_active"] = get_connection(site_id, site.url, site.name) is not None

    elif tab == "content":
        ctx["latest_draft_run"] = (
            db.query(JobRun)
            .filter(JobRun.site_id == site_id, JobRun.kind == "content_draft")
            .order_by(JobRun.created_at.desc()).first()
        )
        ctx["drafts"] = (
            db.query(Content).filter(Content.site_id == site_id)
            .order_by(Content.created_at.desc()).limit(30).all()
        )

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
        body = ""
        if appr.kind == "content":
            try:
                cid = json.loads(appr.payload or "{}").get("content_id")
                content = db.get(Content, cid) if cid else None
                body = content.body if content else ""
            except Exception:
                body = ""
        items.append({"approval": appr, "site": site, "body": body})
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
    if appr.kind == "content":
        try:
            content = db.get(Content, json.loads(appr.payload or "{}").get("content_id"))
            if content:
                content.status = "rejected"
        except Exception:
            pass
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
