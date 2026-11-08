"""Outreach storage on Ascend's database (Postgres on Railway, SQLite locally).

The outreach code was written against sqlite3 (`?` placeholders, dict-like rows). This
adapter keeps that interface: it rewrites `?` to named parameters and the short table
names to their `outreach_` tables, and runs everything through Ascend's SQLAlchemy engine.
"""
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, MetaData, Table, Text, text

from ..database import engine

metadata = MetaData()

Table("outreach_prospects", metadata,
      Column("id", Integer, primary_key=True),
      *[Column(c, Text) for c in (
          "center", "website", "domain", "state", "cities", "levels_of_care", "priority", "website_check",
          "email", "mx_ok", "other_emails", "phone", "first_name", "first_name_source", "status",
          "stage_since", "next_action_at", "notes", "created_at", "updated_at")],
      Column("sheet_row", Integer))
Table("outreach_research", metadata,
      Column("prospect_id", Integer, primary_key=True, autoincrement=False),
      Column("fetched_at", Text), Column("data", Text))
Table("outreach_messages", metadata,
      Column("id", Integer, primary_key=True),
      Column("prospect_id", Integer),
      *[Column(c, Text) for c in (
          "kind", "direction", "status", "to_addr", "from_addr", "subject", "body", "attachment", "message_id",
          "in_reply_to", "refs", "classification", "review_notes", "created_at", "sent_at")])
Table("outreach_serp_checks", metadata,
      Column("id", Integer, primary_key=True),
      Column("prospect_id", Integer),
      *[Column(c, Text) for c in (
          "query", "location", "checked_at", "maps_top", "prospect_in_maps", "organic_top", "prospect_organic", "notes")])
Table("outreach_meta", metadata,
      Column("key", Text, primary_key=True), Column("value", Text))
Table("outreach_events", metadata,
      Column("id", Integer, primary_key=True),
      Column("prospect_id", Integer), Column("at", Text), Column("event", Text), Column("detail", Text))
Table("outreach_files", metadata,
      Column("id", Integer, primary_key=True),
      Column("name", Text), Column("content_b64", Text), Column("created_at", Text))

_TABLES = ["prospects", "research", "messages", "serp_checks", "meta", "events", "files"]
_TABLE_RE = re.compile(r"\b(" + "|".join(_TABLES) + r")\b(?=\s*(?:\(|$|\s|,|;|\)))")
_ready = False


def init():
    global _ready
    if not _ready:
        metadata.create_all(engine)
        _ready = True


def _translate(sql: str, params):
    # Only rewrite table names in SQL keywords context: after FROM/INTO/UPDATE/JOIN/TABLE.
    sql = re.sub(r"(?i)\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+(" + "|".join(_TABLES) + r")\b",
                 lambda m: f"{m.group(1)} outreach_{m.group(2)}", sql)
    named, i = {}, 0
    out = []
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
        if ch == "?" and not in_str:
            out.append(f":p{i}")
            named[f"p{i}"] = params[i]
            i += 1
        else:
            out.append(ch)
    return "".join(out), named


class _Result:
    def __init__(self, res):
        self._res = res

    def fetchone(self):
        if not self._res.returns_rows:
            return None
        r = self._res.mappings().fetchone()
        return dict(r) if r is not None else None

    def fetchall(self):
        if not self._res.returns_rows:
            return []
        return [dict(r) for r in self._res.mappings().fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Con:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        q, named = _translate(sql, list(params))
        return _Result(self._conn.execute(text(q), named))

    def commit(self):
        self._conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    init()
    with engine.connect() as conn:
        con = _Con(conn)
        try:
            yield con
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def del_meta(con, key):
    con.execute("DELETE FROM meta WHERE key=?", (key,))


def log_event(con, prospect_id, event, detail=""):
    con.execute("INSERT INTO events(prospect_id,at,event,detail) VALUES(?,?,?,?)",
                (prospect_id, now_iso(), event, str(detail)[:2000]))


def set_status(con, prospect_id, status, next_action_at=None, note=None):
    con.execute("UPDATE prospects SET status=?, stage_since=?, next_action_at=?, updated_at=? WHERE id=?",
                (status, now_iso(), next_action_at, now_iso(), prospect_id))
    if note:
        con.execute("UPDATE prospects SET notes=COALESCE(notes||' | ','')||? WHERE id=?", (note, prospect_id))
    log_event(con, prospect_id, "status", status + (f" ({note})" if note else ""))
