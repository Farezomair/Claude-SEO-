"""Database models.

Every record that belongs to a site carries a ``site_id`` foreign key. This is
the "private folder per website" rule from the master plan: nothing leaks
between sites because every query is scoped by site_id, and deleting a site
cascades to all of its records.

In Stage 1 these tables exist but stay empty — they are the scaffolding the
agents (Stage 2 onward) will fill. The Site detail page reads from them to show
the Audit / Fixes / Content tabs, which is how we prove isolation works.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base


def utcnow() -> datetime:
    # Naive UTC, so it compares cleanly with values read back from timezone-naive
    # DateTime columns (used by the weekly scheduler's "is it due?" math).
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    url = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    audits = relationship("Audit", back_populates="site", cascade="all, delete-orphan")
    fixes = relationship("Fix", back_populates="site", cascade="all, delete-orphan")
    contents = relationship("Content", back_populates="site", cascade="all, delete-orphan")
    logs = relationship("RunLog", back_populates="site", cascade="all, delete-orphan")


class Audit(Base):
    """One audit run for a site (filled by the Website auditor in Stage 2)."""

    __tablename__ = "audits"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), default="pending")
    summary = Column(Text, default="")
    # Scored audit (the rebuilt auditor): overall 0-100 + letter grade, plus JSON
    # of per-category scores and the prioritized fix plan.
    health_score = Column(Integer, nullable=True)
    grade = Column(String(2), nullable=True)
    category_scores = Column(Text, nullable=True)   # JSON list
    roadmap = Column(Text, nullable=True)           # JSON list
    narrative = Column(Text, nullable=True)         # plain-English health summary
    created_at = Column(DateTime, default=utcnow)

    site = relationship("Site", back_populates="audits")


class Fix(Base):
    """A change the agents made or proposed (filled from Stage 3)."""

    __tablename__ = "fixes"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False)
    status = Column(String(50), default="proposed")  # proposed/applied/verified/failed/reverted
    detail = Column(Text, default="")
    page_ref = Column(String(1000), default="")      # page URL the change applies to
    field = Column(String(100), default="")          # e.g. meta_title, meta_description
    old_value = Column(Text, default="")             # stored so the change is reversible
    new_value = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)

    site = relationship("Site", back_populates="fixes")


class SiteConnection(Base):
    """Per-site WordPress connection, entered in the Settings tab.

    The app password is stored encrypted (see app/crypto.py). Adding a new site
    never touches Railway — the owner pastes the connection here in the browser.
    """

    __tablename__ = "site_connections"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), unique=True, nullable=False)
    wp_url = Column(String(500), default="")
    wp_username = Column(String(255), default="")
    wp_app_password_enc = Column(Text, default="")   # encrypted, never plain text
    updated_at = Column(DateTime, default=utcnow)


class JobRun(Base):
    """A run of an agent for a site (e.g. the SEO technical meta-fix run).

    Tracks running/completed/failed status so the UI can show progress, the
    same way audits do.
    """

    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(50), default="metafix")     # metafix / content_draft
    status = Column(String(50), default="running")   # running/completed/failed
    summary = Column(Text, default="")
    # Live pipeline progress for the Command Center (weekly/Conductor runs).
    phase = Column(String(30), nullable=True)         # audit/route/fix/report/done
    findings_count = Column(Integer, nullable=True)
    fixes_count = Column(Integer, nullable=True)
    # One-by-one Fix-stage progress (the dispatcher updates these per finding).
    progress_done = Column(Integer, nullable=True)
    progress_total = Column(Integer, nullable=True)
    progress_label = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Approval(Base):
    """A proposed change waiting for a human yes/no (the safety gate, Stage 4).

    Risky actions — content publishing especially — create a pending Approval
    instead of going live. The owner approves or rejects on the Approvals
    screen. ``payload`` is JSON describing what to do on approval (e.g. which
    Content row to publish).
    """

    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(50), default="content")
    title = Column(String(500), default="")
    summary = Column(Text, default="")
    payload = Column(Text, default="")              # JSON
    status = Column(String(20), default="pending")  # pending/approved/rejected
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)

    site = relationship("Site")


class Report(Base):
    """A weekly progress summary for a site (Stage 5)."""

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    summary = Column(Text, default="")     # one-line headline
    body_html = Column(Text, default="")   # full report
    created_at = Column(DateTime, default=utcnow)


class SiteChange(Base):
    """A live website change proposed by the Website agent (Stage 6).

    Currently CSS-only (the safe, reversible kind). Stores the new CSS and a
    backup of the previous CSS so any applied change can be reverted in one
    click. Goes through the approval gate before it touches the live site.
    """

    __tablename__ = "site_changes"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(50), default="website_css")
    request = Column(Text, default="")     # what the owner asked for
    css = Column(Text, default="")         # the new value to apply (CSS, or page HTML for page_rewrite)
    old_css = Column(Text, default="")     # backup of the previous value, for one-click revert
    status = Column(String(20), default="proposed")  # proposed/applied/reverted/failed
    # For kind="page_rewrite": which Elementor page + html widget the body lives in.
    target_page_id = Column(Integer, nullable=True)
    target_widget_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class GoogleAuth(Base):
    """A single global Google OAuth connection (Search Console, read-only).

    One Google account's refresh token (encrypted) grants read access to all of
    its Search Console properties; the GSC client matches each site to its
    property at runtime. One-time connect, all sites.
    """

    __tablename__ = "google_auth"

    id = Column(Integer, primary_key=True)
    refresh_token_enc = Column(Text, default="")
    email = Column(String(255), default="")
    updated_at = Column(DateTime, default=utcnow)


class Rulebook(Base):
    """Editable agent rules (Stage 6).

    The dos and don'ts the agents follow, stored as configuration rather than
    hardcoded so they can be tuned without a rebuild. One row per scope:
    'shared' applies to every agent; agent-specific scopes layer on top.
    """

    __tablename__ = "rulebooks"

    id = Column(Integer, primary_key=True)
    scope = Column(String(50), unique=True, nullable=False)  # shared / seo_technical / content
    content = Column(Text, default="")
    updated_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Phase A — the structured architecture spine.
# A Finding is a routed, classified problem produced by an auditor. A FixRecord
# is what a doer did about it, with a rollback snapshot and a verification
# verdict. Every agent in the blueprint plugs into these two tables.
# ---------------------------------------------------------------------------
class Finding(Base):
    __tablename__ = "findings"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    audit_id = Column(Integer, ForeignKey("audits.id", ondelete="CASCADE"), nullable=True)
    finding_key = Column(String(80), default="")     # e.g. WA-{site}-{run}-{seq}
    mode = Column(String(20), default="audit")       # audit / verification
    group = Column(String(50), default="")           # site-integrity, indexation, meta, structure
    category = Column(String(50), default="")        # broken_link, meta_title, ...
    issue = Column(Text, default="")                 # human description
    severity = Column(String(20), default="medium")  # blocker/critical/high/medium/low
    halt = Column(Boolean, default=False)
    finding_type = Column(String(20), default="defect")  # defect / opportunity
    route = Column(String(50), default="")           # owning doer
    action_class = Column(String(30), default="needs-approval")  # auto-safe/needs-approval/needs-human
    evidence_url = Column(String(1000), default="")
    detection_source = Column(String(50), default="crawl")
    status = Column(String(20), default="open")      # open/closed/reopened/escalated/in-progress/no-capability/superseded
    remark = Column(Text, nullable=True)             # per-line doer outcome (what was done / why not)
    created_at = Column(DateTime, default=utcnow)


class FixRecord(Base):
    __tablename__ = "fix_records"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    finding_id = Column(Integer, ForeignKey("findings.id", ondelete="CASCADE"), nullable=True)
    fix_key = Column(String(80), default="")
    doer = Column(String(50), default="")
    action_taken = Column(Text, default="")
    page_ref = Column(String(1000), default="")
    field = Column(String(100), default="")
    before_value = Column(Text, default="")          # snapshot for rollback
    after_value = Column(Text, default="")
    method = Column(String(30), default="")          # auto-safe/gate-approved/autonomous/...
    lane = Column(String(20), default="")            # autonomous/gated/hard-stop
    applied = Column(Boolean, default=False)
    verification_verdict = Column(String(20), default="")  # verified/not_fixed/partial/regressed
    verify_hint = Column(Text, default="")
    outcome_pending = Column(Boolean, default=False)
    status = Column(String(20), default="done")      # done/handed-off/needs-human-input
    created_at = Column(DateTime, default=utcnow)


class Content(Base):
    """Drafted or published content for a site (filled from Stage 4)."""

    __tablename__ = "content"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False)
    status = Column(String(50), default="draft")
    body = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)

    site = relationship("Site", back_populates="contents")


class AuditIssue(Base):
    """A single finding from an audit run (Stage 2 onward).

    Tied to both an audit and a site, so findings are isolated per site and we
    can show the results of one specific audit run.
    """

    __tablename__ = "audit_issues"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    audit_id = Column(Integer, ForeignKey("audits.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(50), nullable=False)   # broken_page, broken_link, structure
    severity = Column(String(20), default="medium")  # high, medium, low
    url = Column(String(1000), default="")           # page where the issue was found
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)


class RunLog(Base):
    """A short log line of what a run did and why — the audit trail."""

    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    site = relationship("Site", back_populates="logs")
