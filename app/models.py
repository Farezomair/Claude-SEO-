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

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
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
