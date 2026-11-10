"""Social settings and the post queue (own table, shares Ascend's database engine)."""
import copy
import json
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, MetaData, Table, Text, text

from ..database import engine
from ..outreach import db as odb

metadata = MetaData()
Table("social_posts", metadata,
      Column("id", Integer, primary_key=True),
      *[Column(c, Text) for c in (
          "platform", "kind", "source_url", "source_title", "slot_at", "message", "image_url", "link",
          "status", "problems", "post_id", "permalink", "error", "created_at", "posted_at")])

# status: planned (drafted, posts itself at slot_at) | posted | skipped (owner) | failed | missed | superseded
DEFAULTS = {
    "enabled": False,
    "site": "",
    "brand": "",
    "audience": "",
    "timezone": "America/New_York",
    "start_date": "",
    "days": ["Mon", "Wed", "Fri"],
    "time": "12:00",
    "skip_dates": [],
    "plan_days_ahead": 7,
    "new_guide_max_age_days": 3,
    "page_repeat_days": 28,
    "evergreen_pages": [],
    "exclude_words": [],
    "banned_words": ["guarantee", "guaranteed", "cure", "#1", "number one"],
    "style_examples": "",
    "utm_source": "facebook",
    "facebook": {"page_id": "", "page_name": "", "graph_version": "v23.0"},
}
_ready = False


def init():
    global _ready
    if not _ready:
        metadata.create_all(engine)
        _ready = True


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    with odb.connect() as con:
        raw = odb.get_meta(con, "social_settings")
    return _merge(DEFAULTS, json.loads(raw) if raw else {})


def save_patch(patch: dict) -> dict:
    with odb.connect() as con:
        raw = odb.get_meta(con, "social_settings")
        cur = json.loads(raw) if raw else {}
        new = _merge(cur, patch)
        odb.set_meta(con, "social_settings", json.dumps(new))
    return _merge(DEFAULTS, new)


def get_meta(key, default=None):
    with odb.connect() as con:
        return odb.get_meta(con, key, default)


def set_meta(key, value):
    with odb.connect() as con:
        odb.set_meta(con, key, value)


def q(sql, **params):
    init()
    with engine.begin() as conn:
        res = conn.execute(text(sql), params)
        return [dict(r) for r in res.mappings().fetchall()] if res.returns_rows else []


def insert(**row):
    row.setdefault("created_at", now_iso())
    cols = ",".join(row)
    vals = ",".join(f":{k}" for k in row)
    q(f"INSERT INTO social_posts({cols}) VALUES({vals})", **row)


def update(pid, **fields):
    sets = ",".join(f"{k}=:{k}" for k in fields)
    q(f"UPDATE social_posts SET {sets} WHERE id=:id", id=pid, **fields)


def posts(where="1=1", order="slot_at", limit=200, **params):
    return q(f"SELECT * FROM social_posts WHERE {where} ORDER BY {order} LIMIT {int(limit)}", **params)


def problems(row) -> list:
    try:
        return json.loads(row.get("problems") or "[]")
    except ValueError:
        return []
