"""One rule for what makes two approvals "the same thing" — enforced at
CREATION time, everywhere an approval is born.

Identity: same site + same kind + the same edit target. The stable target is
the page being edited (`page_id`) or the page type being created (`page_type`);
volatile ids (finding_id, content_id, change_id are regenerated every run) never
count. When there's no stable target, the title is the identity.

Use `add_approval_if_new(...)` instead of `db.add(Approval(...))` — it checks
the pending queue and silently skips the duplicate, so a re-audit can propose
the same fix a hundred times and the owner still sees ONE card.
`collapse_pending_duplicates` is the safety net that de-dupes anything already
in the queue (runs after every dispatch and on the approvals views).
"""
import json

from .models import Approval

_TARGET_KEYS = ("page_id", "page_type")


def _target(payload: dict):
    for k in _TARGET_KEYS:
        if payload.get(k) is not None:
            return (k, payload[k])
    return None


def _payload(a) -> dict:
    try:
        return json.loads(a.payload or "{}") or {}
    except Exception:
        return {}


def pending_duplicate(db, site_id, kind, title="", payload=None):
    """The pending approval this new one would duplicate, or None."""
    payload = payload or {}
    tgt = _target(payload)
    title_key = (title or "").strip().lower()
    for a in (db.query(Approval)
              .filter(Approval.site_id == site_id, Approval.kind == kind,
                      Approval.status == "pending").all()):
        if tgt is not None and _target(_payload(a)) == tgt:
            return a
        if title_key and (a.title or "").strip().lower() == title_key:
            return a
    return None


def add_approval_if_new(db, site_id, kind, title, summary, payload_dict,
                        status="pending"):
    """Insert an Approval unless an equivalent pending one exists.
    Returns (approval, created): the existing card and False on a duplicate."""
    dup = pending_duplicate(db, site_id, kind, title, payload_dict)
    if dup is not None:
        return dup, False
    a = Approval(site_id=site_id, kind=kind, title=title, summary=summary,
                 payload=json.dumps(payload_dict), status=status)
    db.add(a)
    return a, True


def collapse_pending_duplicates(db) -> int:
    """Safety net: keep the newest pending approval per identity (site, kind,
    target-or-title); supersede the rest. Returns how many were collapsed."""
    pend = (db.query(Approval).filter(Approval.status == "pending")
            .order_by(Approval.created_at.desc()).all())  # newest first
    seen_target, seen_title, n = set(), set(), 0
    for a in pend:
        tgt = _target(_payload(a))
        gkey = (a.site_id, a.kind, tgt) if tgt is not None else None
        tkey = (a.site_id, a.kind, (a.title or "").strip().lower())
        if (gkey is not None and gkey in seen_target) or tkey in seen_title:
            a.status = "superseded"
            n += 1
            continue
        if gkey is not None:
            seen_target.add(gkey)
        seen_title.add(tkey)
    if n:
        db.commit()
    return n
