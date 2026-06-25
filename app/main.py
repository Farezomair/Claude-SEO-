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
from .database import Base, engine, get_db
from .jobs import start_audit_async
from .models import Audit, AuditIssue, Site

# Create tables on startup. (Stage 1 has no migrations tool yet; create_all is
# enough while the schema is young. We can add Alembic later if needed.)
Base.metadata.create_all(bind=engine)

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
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    if tab not in {"audit", "fixes", "content", "settings"}:
        tab = "audit"

    latest_audit = None
    issues = []
    if tab == "audit":
        latest_audit = (
            db.query(Audit)
            .filter(Audit.site_id == site_id)
            .order_by(Audit.created_at.desc())
            .first()
        )
        if latest_audit and latest_audit.status == "completed":
            severity_rank = {"high": 0, "medium": 1, "low": 2}
            issues = (
                db.query(AuditIssue)
                .filter(AuditIssue.audit_id == latest_audit.id)
                .all()
            )
            issues.sort(key=lambda i: (severity_rank.get(i.severity, 3), i.category))

    return templates.TemplateResponse(
        "site_detail.html",
        {
            "request": request,
            "site": site,
            "tab": tab,
            "user": current_user(request),
            "latest_audit": latest_audit,
            "issues": issues,
        },
    )


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
