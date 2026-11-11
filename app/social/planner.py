"""Decide what goes out when, draft it a week ahead, and publish it at its time.

Slots: the configured weekdays (default Mon, Wed, Fri) at the configured time (default 12:00 ET), from
start_date, minus skip_dates. A newly published blog post gets a "new guide" post on its publish day
(or the next weekday), replacing that day's evergreen post if there is one. Every other slot gets the
best evergreen post: a blog post's 2-to-3-week diagram post or 4-to-6-week stat post first, then a
service page that hasn't been posted for page_repeat_days, then older blog posts in rotation.
"""
import json
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import facebook, source, store, writer

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LIVE = ("planned", "posted", "handed")  # handed = scheduled in Meta's own planner by the Chrome task
lock = threading.Lock()


def _log(msg):
    print(f"[social] {datetime.utcnow():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _tz(cfg):
    return ZoneInfo(cfg["timezone"])


def _slot_at(d: date, cfg) -> datetime:
    hh, mm = (int(x) for x in cfg["time"].split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_tz(cfg)).astimezone(timezone.utc)


def _row_day(row, cfg) -> date:
    return datetime.fromisoformat(row["slot_at"]).astimezone(_tz(cfg)).date()


def _open_day(d: date, cfg, weekdays_only=True) -> bool:
    if d.isoformat() in cfg["skip_dates"]:
        return False
    if cfg["start_date"] and d.isoformat() < cfg["start_date"]:
        return False
    return d.weekday() < 5 if weekdays_only else DAY_NAMES[d.weekday()] in cfg["days"]


def alert(subject, body):
    try:
        from ..outreach import config as ocfg, mailer
        to = ocfg.load().get("alerts_to")
        if to:
            mailer.send(mailer.build(to, "[Social] " + subject, body))
    except Exception as e:
        _log(f"could not send alert: {e}")


def _fb_rows():
    return store.posts("platform='facebook'", order="slot_at", limit=2000)


def _candidates(d: date, cfg, blog, rows):
    """Evergreen choices for day d, best first: (kind, src_ref)."""
    used = [r for r in rows if r["status"] in ("planned", "posted", "skipped", "failed")]

    def has(url, kind):
        return any(r["source_url"] == url and r["kind"] == kind for r in used)

    def last_day(url):
        ds = [_row_day(r, cfg) for r in rows if r["source_url"] == url and r["status"] in LIVE]
        return max(ds) if ds else None

    def recent(url, days=10):
        ld = last_day(url)
        return ld is not None and abs((d - ld).days) < days

    prev = [r for r in rows if r["status"] in LIVE and _row_day(r, cfg) < d]
    prev_was_page = bool(prev) and prev[-1]["kind"] == "page"
    out = []
    for p in blog:
        age = (d - p["published"].astimezone(_tz(cfg)).date()).days
        if recent(p["url"]):
            continue
        if 14 <= age <= 24 and not has(p["url"], "diagram"):
            out.append((0, age, "diagram", p))
        elif 28 <= age <= 45 and not has(p["url"], "stat"):
            out.append((0, age, "stat", p))
    for pg in cfg["evergreen_pages"]:
        url = pg["url"] if isinstance(pg, dict) else pg
        ld = last_day(url)
        if ld is None or (d - ld).days >= cfg["page_repeat_days"]:
            out.append((3 if prev_was_page else 1, (ld or date(2000, 1, 1)).toordinal(), "page", {"url": url}))
    for p in blog:
        age = (d - p["published"].astimezone(_tz(cfg)).date()).days
        if age < 7 or recent(p["url"], 21):
            continue
        counts = {k: sum(1 for r in used if r["source_url"] == p["url"] and r["kind"] == k) for k in ("diagram", "checklist", "stat")}
        kind = min(counts, key=lambda k: (counts[k], ("diagram", "checklist", "stat").index(k)))
        out.append((2, (last_day(p["url"]) or date(2000, 1, 1)).toordinal(), kind, p))
    out.sort(key=lambda c: (c[0], c[1]))
    return [(k, s) for _, _, k, s in out]


def _draft_into(d: date, kind: str, src: dict, cfg, rows, dry=False):
    if src.get("type") != "post":
        src = source.page(src["url"], cfg["exclude_words"])
        src["images"] = []  # service pages go out as link posts with the page's own preview
    recent = [r["message"] for r in rows if r["status"] in LIVE and r["message"]][-6:]
    used_imgs = {r["image_url"] for r in rows if r["image_url"] and r["status"] in LIVE}
    res = writer.draft(kind, src, cfg, recent, used_imgs)
    row = dict(platform="facebook", kind=kind, source_url=src["url"], source_title=src["title"],
               slot_at=_slot_at(d, cfg).isoformat(), message=res["message"], image_url=res["image_url"],
               link=res["link"], status="failed" if res["problems"] else "planned",
               problems=json.dumps(res["problems"]))
    if not dry:
        store.insert(**row)
    _log(f"drafted {d} {kind} {src['url']}: {row['status']} {res['problems'] or ''}")
    if res["problems"] and not dry:
        alert(f"Couldn't draft the {d:%a %b %d} post",
              f"{kind} post from {src['url']} failed the checks after 3 tries:\n- " + "\n- ".join(res["problems"])
              + "\n\nThe slot will be tried again with another source on the next run.")
    return row


def plan(now: datetime | None = None, dry=False) -> list:
    """Draft everything due in the next plan_days_ahead days. Returns the rows drafted."""
    cfg = store.load()
    if not cfg["site"]:
        return []
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_tz(cfg)).date()
    blog = source.blog_posts(cfg["site"], cfg["exclude_words"])
    rows = _fb_rows()
    made = []

    # 1. New blog posts: a "new guide" post on publish day or the next open weekday.
    for p in blog:
        if any(r["source_url"] == p["url"] and r["kind"] == "new_guide" for r in rows):
            continue
        if now - p["published"] > timedelta(days=cfg["new_guide_max_age_days"]):
            continue
        d = max(p["published"].astimezone(_tz(cfg)).date(), today)
        if cfg["publisher"] == "meta_planner":  # the Chrome task needs a run between drafting and posting
            d = max(d, p["published"].astimezone(_tz(cfg)).date() + timedelta(days=1))
        for _ in range(10):
            taken = [r for r in rows if r["status"] in LIVE and _row_day(r, cfg) == d]
            if _open_day(d, cfg) and _slot_at(d, cfg) > now and not any(r["status"] in ("posted", "handed") or r["kind"] == "new_guide" for r in taken):
                break
            d += timedelta(days=1)
        else:
            continue
        for r in rows:
            if r["status"] == "planned" and _row_day(r, cfg) == d and not dry:
                store.update(r["id"], status="superseded")
                r["status"] = "superseded"
        made.append(_draft_into(d, "new_guide", p, cfg, rows, dry))
        rows = _fb_rows() if not dry else rows + [made[-1]]

    # 2. Regular slots in the planning window.
    for i in range(cfg["plan_days_ahead"] + 1):
        d = today + timedelta(days=i)
        if not _open_day(d, cfg, weekdays_only=False) or _slot_at(d, cfg) <= now:
            continue
        day_rows = [r for r in rows if _row_day(r, cfg) == d]
        if any(r["status"] in LIVE for r in day_rows):
            continue
        if sum(1 for r in day_rows if r["status"] == "failed") >= 2:
            continue
        for kind, src in _candidates(d, cfg, blog, rows)[:1]:
            try:
                made.append(_draft_into(d, kind, src, cfg, rows, dry))
                if dry:
                    rows = rows + [made[-1]]
            except Exception as e:
                _log(f"draft failed {d} {kind}: {e}")
                if not dry:
                    store.insert(platform="facebook", kind=kind, source_url=src["url"], source_title=src.get("title", ""),
                                 slot_at=_slot_at(d, cfg).isoformat(), status="failed", problems=json.dumps([str(e)[:300]]))
            if not dry:
                rows = _fb_rows()
    return made


def post_due(now: datetime | None = None) -> list:
    cfg = store.load()
    now = now or datetime.now(timezone.utc)
    done = []
    for r in store.posts("platform='facebook' AND status='planned' AND slot_at<=:now", now=now.isoformat()):
        late = now - datetime.fromisoformat(r["slot_at"])
        if cfg["publisher"] == "meta_planner":  # the Chrome task should have scheduled it in Meta's planner
            store.update(r["id"], status="missed", error="not scheduled in Meta's planner in time")
            alert("A Facebook post was not scheduled",
                  f"{r['slot_at']} {r['source_url']}\n\nThe Facebook scheduling task (Claude app, Scheduled) didn't "
                  "schedule it in time. Usually the PC was off. Check the task's last run.")
            continue
        if not cfg["enabled"] or not facebook.token():
            if late > timedelta(hours=6):
                store.update(r["id"], status="missed", error="autopilot off or not connected")
            continue
        if late > timedelta(hours=6):
            store.update(r["id"], status="missed", error=f"{late} late (Ascend was down?)")
            alert("A Facebook post was missed", f"{r['slot_at']} {r['source_url']} was not posted in time.")
            continue
        try:
            res = facebook.publish(r["message"], r["image_url"], r["link"])
            store.update(r["id"], status="posted", post_id=res["post_id"], permalink=res["permalink"], posted_at=store.now_iso())
            _log(f"posted #{r['id']} {r['source_url']} -> {res['post_id']}")
            done.append(r["id"])
        except Exception as e:
            store.update(r["id"], status="failed", error=str(e)[:500])
            _log(f"post failed #{r['id']}: {e}")
            reconnect = isinstance(e, facebook.FbError) and e.code == 190
            alert("Facebook post failed" + (": reconnect the page" if reconnect else ""),
                  f"{r['source_url']}\n\n{e}\n\n" + ("The page token stopped working. Open Ascend > Social and connect again."
                                                     if reconnect else "Open Ascend > Social to see it."))
    return done


def import_history(items: list) -> int:
    """Posts made outside Ascend (by hand or already scheduled in Meta's planner), so the rotation knows them."""
    cfg = store.load()
    rows = _fb_rows()
    n = 0
    for h in items:
        d = date.fromisoformat(h["date"])
        if any(r["source_url"] == h["url"] and r["kind"] == h["kind"] and _row_day(r, cfg) == d for r in rows):
            continue
        store.insert(platform="facebook", kind=h["kind"], source_url=h["url"], source_title=h.get("title", ""),
                     slot_at=_slot_at(d, cfg).isoformat(), message=h.get("message", ""), image_url=h.get("image_url", ""),
                     link=h.get("link", ""), status="posted", post_id=h.get("post_id", "outside Ascend"),
                     posted_at=store.now_iso(), problems="[]")
        n += 1
    return n


def run_once():
    if not lock.acquire(blocking=False):
        return
    try:
        last = store.get_meta("social_last_plan")
        if not last or datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(minutes=30):
            store.set_meta("social_last_plan", datetime.now(timezone.utc).isoformat())
            plan()
        post_due()
    except Exception:
        _log("run failed:\n" + traceback.format_exc())
    finally:
        lock.release()


def digest_lines() -> list[str]:
    """For the daily outreach digest: what Facebook did yesterday/today and what's next."""
    cfg = store.load()
    if not cfg["site"]:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    posted = store.posts("platform='facebook' AND status='posted' AND posted_at>=:s AND post_id<>'outside Ascend'", s=since)
    nxt = store.posts("platform='facebook' AND status IN ('planned','handed')", limit=3)
    bad = store.posts("platform='facebook' AND status IN ('failed','missed') AND created_at>=:s", s=since)
    meta = cfg["publisher"] == "meta_planner"
    on = meta or (cfg["enabled"] and facebook.token())
    lines = ["Facebook: " + (("autopilot ON" + (" (Chrome task schedules posts in Meta's planner)" if meta else ""))
                             if on else "autopilot OFF (Ascend > Social)")]
    for r in posted:
        lines.append(f"  posted: {r['source_title'] or r['source_url']} {r['permalink'] or ''}")
    for r in nxt:
        lines.append(f"  next {_row_day(r, cfg):%a %b %d}: {r['kind'].replace('_', ' ')}, {r['source_title'] or r['source_url']}"
                     + (" (in Meta's planner)" if r["status"] == "handed" else ""))
    for r in bad:
        lines.append(f"  PROBLEM {r['status']}: {r['source_url']} {r['error'] or r['problems']}")
    return lines + [""]
