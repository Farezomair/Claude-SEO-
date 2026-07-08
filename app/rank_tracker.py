"""Rank tracker — the outcome loop.

Fix-verification proves a change LANDED; this proves whether it MOVED RANKINGS.
Every weekly run snapshots each target keyword's real Google position (Search
Console, 7-day window, position of the mapped page for its mapped query) into
`rank_snapshots`. The Rankings panel then shows the trend per keyword with the
fixes applied along the way — "position 14 → 8 since the Redirects Agent ran"
instead of a hygiene score moving in a vacuum. Free (GSC only).
"""
from datetime import timedelta

from .database import SessionLocal
from .gsc import queries_by_page
from .models import FixRecord, KeywordTarget, RankSnapshot, RunLog, utcnow


def take_snapshot(site_id: int, site_url: str) -> int:
    """Record a position snapshot for every target keyword that has real GSC
    data (7-day window). Returns rows written (0 = no GSC or no demand yet).
    At most one snapshot per keyword per day (re-runs same-day are no-ops)."""
    gsc = queries_by_page(site_url, days=7, top_n=50)
    if not gsc:
        return 0
    db = SessionLocal()
    try:
        targets = db.query(KeywordTarget).filter(KeywordTarget.site_id == site_id).all()
        if not targets:
            return 0
        cutoff = utcnow() - timedelta(hours=20)
        recent = {s.keyword for s in (db.query(RankSnapshot)
                                      .filter(RankSnapshot.site_id == site_id,
                                              RankSnapshot.created_at >= cutoff).all())}
        written = 0
        for t in targets:
            if t.primary_kw in recent:
                continue
            rows = gsc.get(t.page_path) or []
            hit = next((r for r in rows if r["query"].strip().lower() == t.primary_kw.strip().lower()), None)
            if not hit:
                continue  # Google hasn't shown this page for its target query yet
            db.add(RankSnapshot(
                site_id=site_id, keyword=t.primary_kw, page_path=t.page_path,
                position=int(round(float(hit["position"]) * 10)),
                clicks=int(hit.get("clicks", 0)), impressions=int(hit.get("impressions", 0))))
            written += 1
        if written:
            db.add(RunLog(site_id=site_id,
                          message=f"Rank tracker: recorded positions for {written} target keyword(s)."))
        db.commit()
        return written
    finally:
        db.close()


def _spark(points: list, w: int = 120, h: int = 26) -> str:
    """SVG polyline points for a position sparkline (lower position = higher)."""
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    step = w / (len(points) - 1)
    return " ".join(f"{round(i * step, 1)},{round(3 + (h - 6) * (p - lo) / span, 1)}"
                    for i, p in enumerate(points))


def rank_rows(db, site_id: int) -> list:
    """Panel data: one row per tracked keyword — latest position, trend, delta,
    and how many verified fixes touched its page across the tracked window."""
    snaps = (db.query(RankSnapshot).filter(RankSnapshot.site_id == site_id)
             .order_by(RankSnapshot.created_at.asc()).all())
    if not snaps:
        return []
    by_kw: dict = {}
    for s in snaps:
        by_kw.setdefault(s.keyword, []).append(s)
    first_at = snaps[0].created_at
    fixes = (db.query(FixRecord)
             .filter(FixRecord.site_id == site_id, FixRecord.applied == True,  # noqa: E712
                     FixRecord.created_at >= first_at).all())
    rows = []
    for kw, ss in by_kw.items():
        positions = [s.position / 10 for s in ss if s.position is not None]
        if not positions:
            continue
        latest, first = positions[-1], positions[0]
        path = ss[-1].page_path or ""
        page_fixes = sum(1 for f in fixes if path and path in (f.page_ref or ""))
        rows.append({
            "keyword": kw, "path": path,
            "latest": round(latest, 1), "first": round(first, 1),
            "delta": round(first - latest, 1),   # positive = climbed
            "spark": _spark(positions),
            "snaps": len(positions),
            "clicks": ss[-1].clicks, "impressions": ss[-1].impressions,
            "page_fixes": page_fixes,
        })
    rows.sort(key=lambda r: r["latest"])
    return rows
