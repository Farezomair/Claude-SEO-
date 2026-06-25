"""SEO Agent System — Stage 1: the empty shell.

A web app with a single-owner login, a Websites list, an Add-website button,
and a Postgres-backed workspace per site. No agents yet — this stage exists to
prove we can build, deploy, and store isolated per-site data.
"""
import os

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import check_credentials, current_user
from .connections import get_connection
from .crypto import encrypt
from .database import Base, engine, get_db
from .jobs import start_audit_async
from .migrations import ensure_columns
from .models import Audit, AuditIssue, Fix, JobRun, Site, SiteConnection
from .seo_technical import start_metafix_async
from .wordpress import WordPressClient

# Create tables on startup, then add any columns missing from existing tables.
Base.metadata.create_all(bind=engine)
ensure_columns(engine)

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
    return templates.TemplateResponse(
        "sites.html", {"request": request, "sites": sites, "user": current_user(request)}
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
    if tab not in {"audit", "fixes", "content", "settings"}:
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

    elif tab == "settings":
        ctx["connection"] = (
            db.query(SiteConnection).filter(SiteConnection.site_id == site_id).first()
        )
        ctx["connection_active"] = get_connection(site_id, site.url, site.name) is not None

    return templates.TemplateResponse("site_detail.html", ctx)


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
