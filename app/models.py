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
    return datetime.now(timezone.utc)


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
    status = Column(String(50), default="proposed")
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)

    site = relationship("Site", back_populates="fixes")


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
