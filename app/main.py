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
    apply_html, copy_diff, list_elementor_pages, start_page_rewrite_async,
    verify_change, verify_html, _find_html_widget, read_body, write_body,
)
from .weekly import start_weekly_async
from .dispatcher import start_dispatch_async
from .amend import start_amend_async
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
    same_site="strict",   # mitigate CSRF: cookie not sent on cross-site requests
    https_only=os.getenv("COOKIE_INSECURE") != "1",  # set COOKIE_INSECURE=1 for local http dev
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Bumped on each deploy so we can confirm which build is live (public, no auth).
BUILD = "stresstest-autofix-39"


@app.get("/version")
def version():
    return JSONResponse({"build": BUILD, "auto_pilot": SCHEDULER_ENABLED,
                         "weekly_interval_days": INTERVAL_DAYS})

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
    "in progress": "info", "no capability": "neutral", "superseded": "neutral",
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

import re as _re

_XSS_BLOCK = _re.compile(r"<\s*(script|iframe|object|embed|form)\b[^>]*>.*?<\s*/\s*\1\s*>", _re.I | _re.S)
_XSS_VOID = _re.compile(r"<\s*(script|iframe|object|embed)\b[^>]*/?>", _re.I)
_XSS_ON = _re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", _re.I)
_XSS_JS = _re.compile(r"(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*", _re.I)


def _sanitize(html) -> Markup:
    """Render DB/AI-generated HTML with the main XSS vectors stripped (scripts,
    iframes, inline on*= handlers, javascript: URIs). Used instead of |safe."""
    s = str(html or "")
    s = _XSS_BLOCK.sub("", s)
    s = _XSS_VOID.sub("", s)
    s = _XSS_ON.sub("", s)
    s = _XSS_JS.sub(r"\1=\2", s)
    return Markup(s)


templates.env.filters["sanitize"] = _sanitize


def _flush_cache(conn) -> None:
    """Best-effort LiteSpeed cache flush so a gated live write shows immediately."""
    try:
        AbilitiesClient(conn["url"], conn["username"], conn["app_password"]).run(
            "hostinger-ai-assistant/litespeed-cache-flush", {})
    except Exception:
        pass


def _pkt(dt) -> str:
    """Format a naive-UTC datetime in the owner's timezone (GMT+5, PKT)."""
    if not dt:
        return ""
    return (dt + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M") + " PKT"


templates.env.filters["pkt"] = _pkt


def _mark_stale(db, run, minutes: int = 20) -> None:
    """A run stuck 'running' past `minutes` (worker lost on a recycle) is failed
    so the UI unblocks and the owner can retry. Generous because an inline run with
    page rewrites + image measuring legitimately takes several minutes."""
    if (run and run.status == "running" and run.created_at
            and (utcnow() - run.created_at) > timedelta(minutes=minutes)):
        run.status = "failed"
        run.summary = (run.summary or "") + " (timed out)"
        db.commit()


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


def _build_chart(points: list[dict]):
    """SVG polyline coords for issues + fixes + health-score over time."""
    if len(points) < 2:
        return None
    W, H, pad = 580, 150, 26
    # Issues and fixes share a count axis; score uses its own 0-100 axis.
    count_max = max([p["issues"] for p in points] + [p.get("fixes", 0) for p in points] + [1])
    n = len(points)
    for i, p in enumerate(points):
        p["x"] = round(pad + (W - 2 * pad) * (i / (n - 1)), 1)
        p["yi"] = round(H - pad - (H - 2 * pad) * (p["issues"] / count_max), 1)
        p["yf"] = round(H - pad - (H - 2 * pad) * (p.get("fixes", 0) / count_max), 1)
        p["ys"] = round(H - pad - (H - 2 * pad) * (min(p["score"], 100) / 100), 1)
    return {
        "w": W, "h": H, "max_i": count_max, "points": points,
        "issues_pts": " ".join(f"{p['x']},{p['yi']}" for p in points),
        "fixes_pts": " ".join(f"{p['x']},{p['yf']}" for p in points),
        "score_pts": " ".join(f"{p['x']},{p['ys']}" for p in points),
    }


_PIPELINE_STAGES = ["audit", "route", "fix", "report"]
_PHASE_ORDER = {"audit": 0, "route": 1, "fix": 2, "report": 3, "done": 4}


def _pipeline_state(run, report_id=None) -> dict:
    """Map a weekly/Conductor JobRun to per-stage states for the Command Center."""
    status = run.status if run else "idle"
    phase = run.phase if run else None
    running = status == "running"
    cur = _PHASE_ORDER.get(phase, -1)
    stages = []
    for i, name in enumerate(_PIPELINE_STAGES):
        if status == "failed" and i == cur:
            state = "failed"
        elif status == "completed" or phase == "done" or cur > i:
            state = "done"
        elif cur == i and running:
            state = "active"
        else:
            state = "pending"
        stages.append({"key": name, "state": state})
    return {
        "running": running,
        "status": status,
        "phase": phase,
        "stages": stages,
        "findings": run.findings_count if run else None,
        "fixes": run.fixes_count if run else None,
        "summary": run.summary if run else "",
        "run_id": run.id if run else None,
        "report_id": report_id,
        # One-by-one Fix-stage progress (the dispatcher fills these per finding).
        "fix_done": run.progress_done if run else None,
        "fix_total": run.progress_total if run else None,
        "fix_label": run.progress_label if run else None,
    }


@app.get("/sites/{site_id}/pipeline-status")
def pipeline_status(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Live pipeline state for the Command Center poller (JSON)."""
    if not current_user(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    run = (
        db.query(JobRun).filter(JobRun.site_id == site_id, JobRun.kind == "weekly")
        .order_by(JobRun.created_at.desc()).first()
    )
    report = (
        db.query(Report).filter(Report.site_id == site_id)
        .order_by(Report.created_at.desc()).first()
    )
    state = _pipeline_state(run, report.id if report else None)
    # Live fix log: findings the dispatcher has worked (has a remark), newest first.
    log = (
        db.query(Finding)
        .filter(Finding.site_id == site_id, Finding.remark.isnot(None))
        .order_by(Finding.id.desc()).limit(15).all()
    )
    state["fix_log"] = [
        {"issue": (f.issue or "")[:90], "status": f.status, "remark": f.remark} for f in log
    ]
    return JSONResponse(state)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(
    site_id: int,
    request: Request,
    tab: str = "command",
    notice: str = "",
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return RedirectResponse("/sites", status_code=303)
    # Single-screen app: everything lives on the Command Center.
    tab = "command"

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

    if tab == "command":
        latest_weekly = (
            db.query(JobRun).filter(JobRun.site_id == site_id, JobRun.kind == "weekly")
            .order_by(JobRun.created_at.desc()).first()
        )
        latest_report = (
            db.query(Report).filter(Report.site_id == site_id)
            .order_by(Report.created_at.desc()).first()
        )
        _mark_stale(db, latest_weekly)
        ctx["pipeline"] = _pipeline_state(latest_weekly, latest_report.id if latest_report else None)
        ctx["latest_report"] = latest_report
        ctx["weekly_running"] = bool(latest_weekly and latest_weekly.status == "running")
        ctx["connection_active"] = get_connection(site_id, site.url, site.name) is not None
        latest_audit = (
            db.query(Audit).filter(Audit.site_id == site_id, Audit.status == "completed")
            .order_by(Audit.created_at.desc()).first()
        )
        ctx["audit_score"] = latest_audit.health_score if latest_audit else None
        ctx["audit_grade"] = latest_audit.grade if latest_audit else None
        try:
            ctx["audit_categories"] = json.loads(latest_audit.category_scores) if latest_audit and latest_audit.category_scores else []
            ctx["audit_roadmap"] = json.loads(latest_audit.roadmap) if latest_audit and latest_audit.roadmap else []
        except Exception:
            ctx["audit_categories"], ctx["audit_roadmap"] = [], []
        latest_fix = (
            db.query(JobRun).filter(JobRun.site_id == site_id, JobRun.kind == "fix")
            .order_by(JobRun.created_at.desc()).first()
        )
        _mark_stale(db, latest_fix)
        ctx["fix_running"] = bool(latest_fix and latest_fix.status == "running")
        ctx["latest_fix"] = latest_fix
        ctx["latest_audit"] = latest_audit
        ctx["narrative"] = latest_audit.narrative if latest_audit else ""
        ctx["findings"] = (
            db.query(Finding).filter(Finding.audit_id == latest_audit.id).all()
            if latest_audit else []
        )
        # Improvement chart across past audits.
        hist = (
            db.query(Audit).filter(Audit.site_id == site_id, Audit.status == "completed")
            .order_by(Audit.created_at.asc()).limit(12).all()
        )
        pts = []
        for idx, a in enumerate(hist):
            n = db.query(Finding).filter(Finding.audit_id == a.id).count()
            fq = db.query(FixRecord).filter(
                FixRecord.site_id == site_id, FixRecord.applied == True,  # noqa: E712
                FixRecord.created_at >= a.created_at)
            if idx + 1 < len(hist):
                fq = fq.filter(FixRecord.created_at < hist[idx + 1].created_at)
            pts.append({"date": a.created_at, "score": a.health_score or 0, "issues": n, "fixes": fq.count()})
        ctx["chart"] = _build_chart(pts)
        # Embedded Approvals + Settings panels.
        _collapse_dupe_approvals(db)
        ctx["approval_items"] = _approval_items(db, site_id)
        ctx["human_tasks"] = _human_task_items(db, site_id)
        ctx["connection"] = db.query(SiteConnection).filter(SiteConnection.site_id == site_id).first()
        ctx["google"] = google_oauth.connection()
        ctx["google_configured"] = google_oauth.configured()
        ctx["scheduler_enabled"] = SCHEDULER_ENABLED
        ctx["interval_days"] = INTERVAL_DAYS
        ctx["pending_count"] = len(ctx["approval_items"])

    elif tab == "audit":
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
            try:
                ctx["audit_categories"] = json.loads(latest_audit.category_scores) if latest_audit.category_scores else []
                ctx["audit_roadmap"] = json.loads(latest_audit.roadmap) if latest_audit.roadmap else []
            except Exception:
                ctx["audit_categories"], ctx["audit_roadmap"] = [], []
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
        # Latest scored audit, shown at the top of the report.
        latest_audit = (
            db.query(Audit).filter(Audit.site_id == site_id, Audit.status == "completed")
            .order_by(Audit.created_at.desc()).first()
        )
        ctx["audit_score"] = latest_audit.health_score if latest_audit else None
        ctx["audit_grade"] = latest_audit.grade if latest_audit else None
        try:
            ctx["audit_categories"] = json.loads(latest_audit.category_scores) if latest_audit and latest_audit.category_scores else []
            ctx["audit_roadmap"] = json.loads(latest_audit.roadmap) if latest_audit and latest_audit.roadmap else []
        except Exception:
            ctx["audit_categories"], ctx["audit_roadmap"] = [], []
        # Improvement chart: issues + health score across past audits (oldest→newest).
        hist = (
            db.query(Audit).filter(Audit.site_id == site_id, Audit.status == "completed")
            .order_by(Audit.created_at.asc()).limit(12).all()
        )
        points = []
        for idx, a in enumerate(hist):
            n = db.query(Finding).filter(Finding.audit_id == a.id).count()
            # Fixes applied in this run's window (from this audit until the next).
            fq = db.query(FixRecord).filter(
                FixRecord.site_id == site_id, FixRecord.applied == True,  # noqa: E712
                FixRecord.created_at >= a.created_at)
            if idx + 1 < len(hist):
                fq = fq.filter(FixRecord.created_at < hist[idx + 1].created_at)
            points.append({"date": a.created_at, "score": a.health_score or 0,
                           "issues": n, "fixes": fq.count()})
        ctx["chart"] = _build_chart(points)
        # "What changed — before & after" feed for the report view.
        recs = (
            db.query(FixRecord)
            .filter(FixRecord.site_id == site_id, FixRecord.applied == True)  # noqa: E712
            .order_by(FixRecord.created_at.desc()).limit(15).all()
        )
        changes = []
        for fr in recs:
            is_page = fr.field == "page_html"
            changes.append({
                "doer": fr.doer, "field": (fr.field or "").replace("_", " "),
                "page_ref": fr.page_ref, "when": fr.created_at,
                "verdict": fr.verification_verdict, "is_page": is_page,
                "before": fr.before_value or "", "after": fr.after_value or "",
                "diff": copy_diff(fr.before_value or "", fr.after_value or "")[:40] if is_page else [],
            })
        ctx["changes"] = changes

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

    return RedirectResponse(f"/sites/{site_id}?tab=command", status_code=303)


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
            reverted_ok = True
            if change.kind in ("page_rewrite", "schema_inject", "img_dims"):
                # Restore the previous _meridian_body (the live render source).
                client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
                reverted_ok = write_body(client, change.target_page_id, change.old_css or "")
            else:
                WordPressClient(conn["url"], conn["username"], conn["app_password"]).update_custom_css(change.old_css)
            if not reverted_ok:
                # Don't tell the owner it's back to normal if we can't confirm it live.
                db.add(RunLog(site_id=site_id,
                              message=f"Revert wrote but couldn't confirm it live (still 'applied'): {change.request[:80]}"))
                db.commit()
                return RedirectResponse(f"/sites/{site_id}?tab=website&notice=revert_fail", status_code=303)
            change.status = "reverted"
            db.add(RunLog(site_id=site_id, message=f"Reverted change: {change.request[:80]}"))
            db.commit()
        except Exception as exc:  # incl. raw httpx transport errors — never 500 a revert
            db.add(RunLog(site_id=site_id,
                          message=f"Revert failed for “{change.request[:60]}”: {exc.__class__.__name__}"))
            db.commit()
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


@app.post("/sites/{site_id}/run-fixes")
def run_fixes(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Dispatcher: apply safe fixes now and send risky ones to Approvals."""
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
        .filter(JobRun.site_id == site_id, JobRun.kind == "fix", JobRun.status == "running")
        .first()
    )
    if not already:
        run = JobRun(site_id=site_id, kind="fix", status="running", summary="Applying fixes…")
        db.add(run)
        db.commit()
        db.refresh(run)
        start_dispatch_async(site_id, run.id)
    return RedirectResponse(f"/sites/{site_id}?tab=command", status_code=303)


@app.post("/sites/{site_id}/stop-run")
def stop_run(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Cancel any in-flight runs for this site so the owner can start fresh."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    kinds = ("weekly", "fix", "metafix", "elementor", "image", "schema", "dedupe", "pagedraft")
    for run in (db.query(JobRun).filter(JobRun.site_id == site_id, JobRun.kind.in_(kinds),
                                        JobRun.status == "running").all()):
        run.status = "cancelled"
        run.summary = "Stopped by you."
    db.add(RunLog(site_id=site_id, message="Run(s) stopped by the owner."))
    db.commit()
    return RedirectResponse(f"/sites/{site_id}?tab=command&notice=stopped", status_code=303)


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
    # There is no `elementor-list-pages` ability; list published pages via REST.
    out["list_pages"] = list_elementor_pages(conn)

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


@app.get("/sites/{site_id}/write-test")
def write_test(site_id: int, request: Request, page_id: int = 12, db: Session = Depends(get_db)):
    """Safe write-path self-test: append an INVISIBLE HTML comment to a page's html
    widget, check which write method actually lands (widget-content vs _elementor_data),
    then auto-revert. Returns a per-step report. Net-zero, no visible change."""
    import time as _time
    from .elementor_agent import (
        _find_html_widget, _elementor_data_of, _set_widget_html, plugin_set_widget,
        A_UPDATE_CONTENT, A_PAGE_GET, A_PAGE_UPDATE, A_CACHE_FLUSH)
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return JSONResponse({"error": "site not found"}, status_code=404)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return JSONResponse({"error": "no WordPress connection"}, status_code=400)
    client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
    steps, result = [], {"page_id": page_id}

    def step(name, fn):
        try:
            r = fn()
            steps.append({"step": name, "ok": True})
            return r
        except Exception as e:
            steps.append({"step": name, "ok": False, "error": f"{e.__class__.__name__}: {str(e)[:300]}"})
            return None

    wh = step("find_widget", lambda: _find_html_widget(client, page_id))
    if not wh or not wh[0]:
        return JSONResponse({"result": result, "steps": steps, "verdict": "no html widget found"})
    widget_id, original = wh
    result.update(widget_id=widget_id, orig_len=len(original))

    # Diagnostic: what does the page's post_content look like, and what does the
    # PUBLIC front end actually serve? (the page may render raw post_content, not
    # the Elementor widget we edit.)
    import re as _re
    import httpx as _httpx
    def _imgstats(s):
        t = _re.findall(r"<img\b[^>]*>", s or "", _re.I)
        sized = sum(1 for x in t if _re.search(r"\swidth\s*=", x, _re.I) and _re.search(r"\sheight\s*=", x, _re.I))
        return {"len": len(s or ""), "imgs": len(t), "sized": sized}
    pg = step("pages_get_content", lambda: client.read(A_PAGE_GET, {"id": page_id}))
    if pg:
        c = pg.get("content")
        result["content_raw"] = _imgstats(c.get("raw")) if isinstance(c, dict) else None
        result["content_rendered"] = _imgstats(c.get("rendered")) if isinstance(c, dict) else _imgstats(c)
        result["edit_mode"] = (pg.get("meta") or {}).get("_elementor_edit_mode")

    def _public():
        with _httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as hc:
            r = hc.get(site.url)
            return {**_imgstats(r.text), "ls_cache": r.headers.get("x-litespeed-cache", "")}
    result["public_render"] = step("public_fetch", _public)

    token = f"<!-- ascend-write-test-{int(_time.time())} -->"
    marked = original + token

    # Preferred path: the SEO Agent Bridge helper plugin (PHP edits _elementor_data).
    pv = step("plugin_write", lambda: plugin_set_widget(client, page_id, widget_id, marked))
    live0 = step("reread_plugin", lambda: _find_html_widget(client, page_id)[1])
    result["plugin_installed"] = bool(pv) or None
    result["plugin_works"] = bool(live0 and token in live0)

    step("widget_content_write", lambda: client.run(
        A_UPDATE_CONTENT, {"post_id": page_id, "widget_id": widget_id, "content": marked}))
    step("cache_flush_1", lambda: client.run(A_CACHE_FLUSH, {}))
    live1 = step("reread_1", lambda: _find_html_widget(client, page_id)[1])
    result["widget_content_works"] = bool(live1 and token in live1)

    if not result["widget_content_works"]:
        page = step("pages_get", lambda: client.read(A_PAGE_GET, {"id": page_id}))
        ed = _elementor_data_of(page or {})
        result["pages_get_returns_elementor_data"] = bool(ed)
        result["elementor_data_len"] = len(ed) if ed else 0

        def ed_write():
            data = json.loads(ed)
            if not _set_widget_html(data, widget_id, marked):
                raise RuntimeError("widget id not present in _elementor_data")
            return client.run(A_PAGE_UPDATE, {"id": page_id,
                                              "meta": {"_elementor_data": json.dumps(data, ensure_ascii=False)}})
        if ed:
            step("elementor_data_write", ed_write)
            step("cache_flush_2", lambda: client.run(A_CACHE_FLUSH, {}))
            live2 = step("reread_2", lambda: _find_html_widget(client, page_id)[1])
            result["elementor_data_works"] = bool(live2 and token in live2)

    # Revert via whichever paths exist (best-effort), then confirm the marker is gone.
    def revert():
        try:
            plugin_set_widget(client, page_id, widget_id, original)
        except Exception:
            pass
        try:
            client.run(A_UPDATE_CONTENT, {"post_id": page_id, "widget_id": widget_id, "content": original})
        except Exception:
            pass
        try:
            page = client.read(A_PAGE_GET, {"id": page_id})
            ed = _elementor_data_of(page or {})
            if ed:
                data = json.loads(ed)
                _set_widget_html(data, widget_id, original)
                client.run(A_PAGE_UPDATE, {"id": page_id,
                                           "meta": {"_elementor_data": json.dumps(data, ensure_ascii=False)}})
        except Exception:
            pass
        try:
            client.run(A_CACHE_FLUSH, {})
        except Exception:
            pass
        return True
    step("revert", revert)
    live3 = step("reread_after_revert", lambda: _find_html_widget(client, page_id)[1])
    result["token_gone_after_revert"] = bool(live3 is not None and token not in (live3 or ""))
    # Native page-save to force a server-cache purge (the path apply_html now uses).
    step("pages_update_purge", lambda: client.run(A_PAGE_UPDATE, {"id": page_id}))
    result["verdict"] = ("plugin" if result.get("plugin_works")
                         else "widget-content" if result.get("widget_content_works")
                         else "elementor-data" if result.get("elementor_data_works")
                         else "NO WRITE METHOD WORKS")
    return JSONResponse({"result": result, "steps": steps})


@app.get("/sites/{site_id}/body-test")
def body_test(site_id: int, request: Request, page_id: int = 12, db: Session = Depends(get_db)):
    """Prove the REAL render path end-to-end: write an INVISIBLE marker into the
    `_meridian_body` field the theme prints, confirm it appears on the PUBLIC page
    (so the field renders + cache purges), then revert. Net-zero."""
    import time as _time
    import re as _re
    import httpx as _httpx
    from .elementor_agent import read_body, write_body
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        return JSONResponse({"error": "site not found"}, status_code=404)
    conn = get_connection(site_id, site.url, site.name)
    if not conn:
        return JSONResponse({"error": "no WordPress connection"}, status_code=400)
    client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
    steps, result = [], {"page_id": page_id}

    def step(name, fn):
        try:
            r = fn(); steps.append({"step": name, "ok": True}); return r
        except Exception as e:
            steps.append({"step": name, "ok": False, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}); return None

    original = step("read_body", lambda: read_body(client, page_id))
    if original is None:
        return JSONResponse({"result": result, "steps": steps,
                             "verdict": "body endpoint unavailable — is SEO Agent Bridge 4 active?"})
    result["body_len"] = len(original)
    token = f"<!-- ascend-body-test-{int(_time.time())} -->"
    marked = original + token

    result["write_verified"] = bool(step("write_body_marked", lambda: write_body(client, page_id, marked)))

    def public_has():
        with _httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as hc:
            r = hc.get(site.url)
            return {"token_live": token in r.text, "ls_cache": r.headers.get("x-litespeed-cache", ""),
                    "bytes": len(r.text)}
    result["public"] = step("public_fetch", public_has)

    step("revert_body", lambda: write_body(client, page_id, original))
    chk = step("read_after_revert", lambda: read_body(client, page_id))
    result["reverted_clean"] = bool(chk is not None and token not in chk)
    result["verdict"] = ("RENDERS LIVE (body field is the source)"
                         if (result.get("public") or {}).get("token_live")
                         else "wrote+verified in field, but NOT visible on public page (cache?)")
    return JSONResponse({"result": result, "steps": steps})


# --------------------------------------------------------------------------
# Approvals (the safety gate)
# --------------------------------------------------------------------------
def _collapse_dupe_approvals(db) -> None:
    """Keep the newest pending approval per (site, kind, title); supersede the rest."""
    dupes = (db.query(Approval).filter(Approval.status == "pending")
             .order_by(Approval.created_at.desc()).all())
    seen, changed = set(), False
    for a in dupes:
        key = (a.site_id, a.kind, a.title)
        if key in seen:
            a.status = "superseded"
            changed = True
        else:
            seen.add(key)
    if changed:
        db.commit()


def _human_task_items(db, site_id=None) -> list:
    q = (db.query(Finding, Site).join(Site, Finding.site_id == Site.id)
         .filter(Finding.status == "needs-human"))
    if site_id:
        q = q.filter(Finding.site_id == site_id)
    return [{"finding": f, "site": s} for f, s in q.order_by(Finding.created_at.desc()).all()]


def _approval_items(db, site_id=None) -> list:
    q = (db.query(Approval, Site).join(Site, Approval.site_id == Site.id)
         .filter(Approval.status == "pending"))
    if site_id:
        q = q.filter(Approval.site_id == site_id)
    items = []
    for appr, site in q.order_by(Approval.created_at.desc()).all():
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
        elif appr.kind == "schema_inject":
            code = payload.get("jsonld", "")
        items.append({"approval": appr, "site": site, "body": body, "code": code,
                      "preview_html": preview_html, "text_diff": text_diff})
    return items


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, notice: str = "", db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    _collapse_dupe_approvals(db)
    items = _approval_items(db)
    human_tasks = _human_task_items(db)
    return templates.TemplateResponse(
        "approvals.html",
        {"request": request, "user": current_user(request), "items": items, "notice": notice,
         "human_tasks": human_tasks, "pending_count": len(items),
         "publish_status": CONTENT_PUBLISH_STATUS},
    )


@app.post("/findings/{finding_id}/done")
def finding_done(finding_id: int, request: Request, db: Session = Depends(get_db)):
    """Owner marks a human-task finding as handled (clears it; re-audit re-checks)."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    f = db.get(Finding, finding_id)
    if f and f.status == "needs-human":
        f.status = "closed"
        f.remark = "Marked done by you — the next audit will re-check."
        db.add(RunLog(site_id=f.site_id, message=f"Owner marked human-task #{f.id} done."))
        db.commit()
    return RedirectResponse("/approvals?notice=task_done", status_code=303)


@app.post("/findings/{finding_id}/snooze")
def finding_snooze(finding_id: int, request: Request, db: Session = Depends(get_db)):
    """Owner defers a human-task finding (hidden now; reappears after the next audit)."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    f = db.get(Finding, finding_id)
    if f and f.status == "needs-human":
        f.status = "snoozed"
        f.remark = "You chose to do this later — it will reappear after the next audit if still unfixed."
        db.commit()
    return RedirectResponse("/approvals?notice=task_snoozed", status_code=303)


def _safe_return(return_to: str, default: str = "/approvals") -> str:
    """Only allow same-site relative redirects (never an open redirect)."""
    rt = (return_to or "").strip()
    return rt if (rt.startswith("/") and not rt.startswith("//")) else default


def _with_notice(url: str, notice: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}notice={notice}"


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: int, request: Request, publish: int = Form(0),
            return_to: str = Form(""), db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    ret = _safe_return(return_to)
    appr = db.get(Approval, approval_id)
    if not appr or appr.status != "pending":
        return RedirectResponse(ret, status_code=303)

    site = db.get(Site, appr.site_id)
    if appr.kind == "content":
        payload = json.loads(appr.payload or "{}")
        content = db.get(Content, payload.get("content_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        status = "publish" if publish else CONTENT_PUBLISH_STATUS
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_post(content.title, content.body, status=status, excerpt=appr.summary)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Publish failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        content.status = "published" if status == "publish" else "in_wordpress_draft"
        db.add(RunLog(
            site_id=site.id,
            message=f"Approved & sent to WordPress ({result.get('status')}): {content.title} {result.get('link', '')}",
        ))

    elif appr.kind == "meta_rewrite":
        payload = json.loads(appr.payload or "{}")
        conn = get_connection(site.id, site.url, site.name)
        if not conn:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
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
            verified = (
                (not new_title or (live.get(YOAST_TITLE_KEY) or "").strip() == new_title.strip())
                and (not new_desc or (live.get(YOAST_DESC_KEY) or "").strip() == new_desc.strip())
            )
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Meta rewrite failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        _flush_cache(conn)
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
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
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
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        _flush_cache(conn)
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
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        status = "publish" if publish else "draft"
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            result = wp.create_page(content.title, content.body, status=status)
        except WordPressError as exc:
            db.add(RunLog(site_id=site.id, message=f"Page create failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        content.status = "published" if status == "publish" else "in_wordpress_draft"
        # A draft page still 404s for visitors and the crawler, so only clear the
        # finding when it's actually published live; a draft re-surfaces next audit.
        if publish:
            finding = db.get(Finding, payload.get("finding_id"))
            if finding:
                finding.status = "closed"
        db.add(RunLog(site_id=site.id,
                      message=f"Created page ({status}) in WordPress: {content.title} {result.get('link', '')}"))

    elif appr.kind == "website_css":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        try:
            wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
            change.old_css = wp.get_custom_css()  # back up the actual current CSS now
            wp.update_custom_css(change.css)
        except WordPressError as exc:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Website change failed for “{appr.title}”: {exc}"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        _flush_cache(conn)
        change.status = "applied"
        db.add(RunLog(site_id=site.id, message=f"Applied website change: {change.request[:80]}"))

    elif appr.kind == "page_rewrite":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        # Write the LIVE render source (_meridian_body), snapshotting it first for revert.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Page rewrite failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        if live:
            change.old_css = live
        verified = write_body(client, page_id, change.css)
        if not verified:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Page rewrite write didn't verify for “{appr.title}”."))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        change.status = "applied"
        db.add(FixRecord(
            site_id=site.id, doer="Elementor On-page",
            action_taken=f"Full-page SEO rewrite of “{appr.title}” (via _meridian_body)",
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

    elif appr.kind == "schema_inject":
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        jstr = payload.get("jsonld", "")
        # Re-read the LIVE body and layer the schema onto CURRENT content so we don't
        # clobber other edits (e.g. image dimensions) made since proposing.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Schema injection failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        from .schema_agent import _has_entity_schema
        change.old_css = live
        if jstr and not _has_entity_schema(live):
            change.css = live + f'\n<script type="application/ld+json">\n{jstr}\n</script>\n'
        else:
            change.css = live  # already has entity schema / nothing to add — no-op write
        if not write_body(client, page_id, change.css):
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Schema write didn't verify for “{appr.title}”."))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        verified = bool(jstr)
        change.status = "applied"
        if verified:
            for f in db.query(Finding).filter(
                    Finding.site_id == site.id,
                    Finding.category.in_(("no_entity_schema", "no_localbusiness_schema")),
                    Finding.status.in_(("open", "in-progress"))).all():
                f.status = "closed"
        db.add(FixRecord(
            site_id=site.id, doer="SEO Technical",
            action_taken="Injected homepage entity schema (via _meridian_body)",
            page_ref=str(page_id), field="schema",
            before_value="(no entity schema)", after_value=payload.get("jsonld", "")[:5000],
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
        ))
        db.add(RunLog(site_id=site.id,
                      message=f"Applied homepage schema: {appr.title} "
                              f"({'verified live' if verified else 'apply ok, verify pending'})"))

    elif appr.kind == "img_dims":
        from .image_agent import _add_dims
        payload = json.loads(appr.payload or "{}")
        change = db.get(SiteChange, payload.get("change_id"))
        conn = get_connection(site.id, site.url, site.name)
        if not conn or not change:
            return RedirectResponse(_with_notice(ret, "no_connection"), status_code=303)
        page_id = change.target_page_id or payload.get("page_id")
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        sizes = payload.get("sizes") or {}
        # Re-read the LIVE body and re-inject dimensions into CURRENT content,
        # snapshotting a true revert point.
        live = read_body(client, page_id)
        if live is None:
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Image-dimension fix failed for “{appr.title}”: couldn't read the page body (Bridge v4+ active?)"))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        change.old_css = live
        change.css = _add_dims(live, sizes) if sizes else live
        if not write_body(client, page_id, change.css):
            change.status = "failed"
            db.add(RunLog(site_id=site.id, message=f"Image-dimension write didn't verify for “{appr.title}”."))
            db.commit()
            return RedirectResponse(_with_notice(ret, "publish_fail"), status_code=303)
        verified = True
        change.status = "applied"
        # Close the in-progress image-dimension finding(s); any still missing on
        # other pages re-detect on the next audit.
        for f in db.query(Finding).filter(
                Finding.site_id == site.id, Finding.category == "image_no_dimensions",
                Finding.status == "in-progress").all():
            f.status = "closed"
        db.add(FixRecord(
            site_id=site.id, doer="Website Agent",
            action_taken=f"Added image dimensions ({payload.get('count', '?')} images, via _meridian_body)",
            page_ref=str(page_id), field="image_no_dimensions",
            before_value="(no width/height)", after_value=f"{payload.get('count', '?')} images sized",
            method="gate-approved", lane="gated", applied=True,
            verification_verdict="verified" if verified else "not_fixed", status="done",
        ))
        db.add(RunLog(site_id=site.id,
                      message=f"Applied image dimensions: {appr.title} "
                              f"({'verified live' if verified else 'apply ok, verify pending'})"))

    appr.status = "approved"
    appr.decided_at = utcnow()
    db.commit()
    return RedirectResponse(_with_notice(ret, "approved"), status_code=303)


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: int, request: Request, return_to: str = Form(""),
           db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    ret = _safe_return(return_to)
    appr = db.get(Approval, approval_id)
    if not appr or appr.status != "pending":
        return RedirectResponse(ret, status_code=303)
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
    elif appr.kind in ("website_css", "page_rewrite", "schema_inject", "img_dims"):
        change = db.get(SiteChange, payload.get("change_id")) if payload.get("change_id") else None
        if change:
            change.status = "rejected"
    appr.status = "rejected"
    appr.decided_at = utcnow()
    db.commit()
    return RedirectResponse(_with_notice(ret, "rejected"), status_code=303)


@app.post("/approvals/{approval_id}/amend")
def amend(approval_id: int, request: Request, instructions: str = Form(""),
          return_to: str = Form(""), db: Session = Depends(get_db)):
    """Owner asks the AI to revise a proposal ('Request amendment'). Regenerates the
    proposal in the background with their feedback; it stays pending for re-review."""
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    ret = _safe_return(return_to)
    appr = db.get(Approval, approval_id)
    instructions = (instructions or "").strip()
    if not appr or appr.status != "pending" or not instructions:
        return RedirectResponse(_with_notice(ret, "amend_empty" if appr else "rejected"), status_code=303)
    appr.amend_note = "⏳ Reworking this with your changes — refresh in a moment…"
    db.add(RunLog(site_id=appr.site_id, message=f"Amendment requested for “{appr.title}”: {instructions[:160]}"))
    db.commit()
    start_amend_async(approval_id, instructions)
    return RedirectResponse(_with_notice(ret, "amending"), status_code=303)


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
