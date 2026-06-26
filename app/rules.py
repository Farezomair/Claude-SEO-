"""Editable agent rulebooks (Stage 6).

Agents read their rules on every run and weave them into the Claude prompt, so
the owner can tune behavior ("never mention competitors", "keep a friendly
tone", "always include the city name") without a code change.
"""
from .database import SessionLocal
from .models import Rulebook, utcnow

# (scope, label) — drives the Rules editing page.
SCOPES = [
    ("shared", "Shared rules — apply to every agent"),
    ("seo_technical", "SEO technical agent — meta titles & descriptions"),
    ("content", "Content writer — blog posts"),
]


def get_rules(scope: str) -> str:
    db = SessionLocal()
    try:
        row = db.query(Rulebook).filter(Rulebook.scope == scope).first()
        return row.content if row and row.content else ""
    finally:
        db.close()


def rules_for(*scopes: str) -> str:
    """Combine several scopes (e.g. shared + agent) into one prompt block."""
    parts = [get_rules(s).strip() for s in scopes]
    return "\n".join(p for p in parts if p)


def set_rules(scope: str, content: str) -> None:
    db = SessionLocal()
    try:
        row = db.query(Rulebook).filter(Rulebook.scope == scope).first()
        if not row:
            row = Rulebook(scope=scope)
            db.add(row)
        row.content = content
        row.updated_at = utcnow()
        db.commit()
    finally:
        db.close()
