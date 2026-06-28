"""Tiny additive schema migration.

create_all() creates new tables but does not add columns to tables that already
exist. As the schema grows stage by stage, this helper adds any missing columns
to existing tables with a plain ALTER TABLE (works on both Postgres and SQLite
for simple ADD COLUMN). It only ever adds columns — never drops or alters — so
it is safe to run on every startup.
"""
from sqlalchemy import inspect, text

# table -> {column_name: column_type_ddl}
WANTED_COLUMNS = {
    "fixes": {
        "page_ref": "VARCHAR(1000)",
        "field": "VARCHAR(100)",
        "old_value": "TEXT",
        "new_value": "TEXT",
    },
    # Elementor page-rewrite targets, so an applied rewrite can be reverted.
    "site_changes": {
        "target_page_id": "INTEGER",
        "target_widget_id": "VARCHAR(100)",
    },
    # Live pipeline progress for the Command Center.
    "job_runs": {
        "phase": "VARCHAR(30)",
        "findings_count": "INTEGER",
        "fixes_count": "INTEGER",
        "progress_done": "INTEGER",
        "progress_total": "INTEGER",
        "progress_label": "VARCHAR(300)",
    },
    "findings": {
        "remark": "TEXT",
    },
    # Scored audit (rebuilt auditor).
    "audits": {
        "health_score": "INTEGER",
        "grade": "VARCHAR(2)",
        "category_scores": "TEXT",
        "roadmap": "TEXT",
    },
}


def ensure_columns(engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in WANTED_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
