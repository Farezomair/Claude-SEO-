"""SEO technical agent (Stage 3) — meta title/description fixes.

Flow per run, against the live WordPress site (Yoast):
1. Scan published pages/posts for missing / too-short / too-long meta titles
   and descriptions.
2. For each page that needs work, ask Claude to write a better title/description
   from the page's actual content.
3. Store the OLD value (reversible), then write the new one via the REST API.
4. QC: re-read the page and confirm the new value is live before marking the
   fix verified.

Guardrails: a hard cap on the number of fixes per run (MAX_FIXES) and a
verify-before-done QC step. Safe, reversible changes only — no approval gate.
"""
import threading

import json

from .brain import generate_meta
from .database import SessionLocal
from .models import Approval, Finding, FixRecord, JobRun, RunLog, Site
from .routing import classify
from .rules import rules_for
from .wordpress import YOAST_DESC_KEY, YOAST_TITLE_KEY, WordPressClient, WordPressError

# Hard caps / thresholds (guardrails).
MAX_FIXES = 10
TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160


def _evaluate(meta: dict) -> list[tuple[str, str]]:
    """Return [(field, reason), ...] for meta that should be improved."""
    issues = []
    title = (meta.get(YOAST_TITLE_KEY) or "").strip()
    desc = (meta.get(YOAST_DESC_KEY) or "").strip()
    # A Yoast title containing %%placeholders%% is the default template, not a
    # real custom title — treat it as missing.
    if not title or "%%" in title:
        issues.append(("meta_title", "no custom title set"))
    elif len(title) > TITLE_MAX:
        issues.append(("meta_title", f"too long ({len(title)} chars)"))
    elif len(title) < TITLE_MIN:
        issues.append(("meta_title", f"too short ({len(title)} chars)"))

    if not desc:
        issues.append(("meta_description", "missing"))
    elif len(desc) > DESC_MAX:
        issues.append(("meta_description", f"too long ({len(desc)} chars)"))
    elif len(desc) < DESC_MIN:
        issues.append(("meta_description", f"too short ({len(desc)} chars)"))
    return issues


def run_metafix(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    fixes_made = 0
    pages_scanned = 0
    try:
        run = db.get(JobRun, run_id)
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])

        ok, code = wp.test()
        if not ok:
            run.status = "failed"
            run.summary = f"WordPress connection failed (HTTP {code}). Check the connection in Settings."
            db.add(RunLog(site_id=site_id, message=f"Meta-fix aborted: WP auth failed (HTTP {code})."))
            db.commit()
            return

        rules = rules_for("shared", "seo_technical")
        items = wp.list_content(limit=60)
        for item in items:
            if fixes_made >= MAX_FIXES:
                break
            pages_scanned += 1
            issues = _evaluate(item["meta"])
            if not issues:
                continue

            # One generation per page covers both title and description.
            try:
                suggestion = generate_meta(
                    item["title"], item["link"], item["content_text"],
                    conn.get("site_name", ""), rules=rules,
                )
            except Exception as exc:
                db.add(RunLog(site_id=site_id,
                              message=f"Skipped {item['link']}: meta generation failed ({exc.__class__.__name__})."))
                db.commit()
                continue

            for field, reason in issues:
                if fixes_made >= MAX_FIXES:
                    break
                if field == "meta_title":
                    key, new_val = YOAST_TITLE_KEY, suggestion["title"]
                else:
                    key, new_val = YOAST_DESC_KEY, suggestion["description"]
                if not new_val:
                    continue
                old_val = (item["meta"].get(key) or "").strip()

                # Record the routed Finding (in Phase B this comes from the SEO
                # Auditor; for now the SEO Technical doer detects + fixes it).
                cls = classify(field)
                finding = Finding(
                    site_id=site_id, finding_key=f"SA-{site_id}-{run_id}-{fixes_made + 1}",
                    mode="audit", group=cls["group"], category=field,
                    issue=f"{field.replace('_', ' ')} {reason} on {item['link']}",
                    severity="medium", route=cls["route"], action_class=cls["action_class"],
                    evidence_url=item["link"], detection_source="crawl", status="open",
                )
                db.add(finding)
                db.commit()
                db.refresh(finding)

                fix_key = f"FX-{site_id}-{run_id}-{fixes_made + 1}"
                # Verify-before-write reversibility: capture old, then write.
                try:
                    wp.update_meta(item["kind"], item["id"], {key: new_val})
                except WordPressError as exc:
                    db.add(FixRecord(
                        site_id=site_id, finding_id=finding.id, fix_key=fix_key,
                        doer="SEO Technical", action_taken=f"Write {field} failed: {exc}",
                        page_ref=item["link"], field=field, before_value=old_val,
                        after_value=new_val, method="auto-safe", lane="autonomous",
                        applied=False, status="handed-off",
                    ))
                    finding.status = "escalated"
                    db.commit()
                    continue

                # Verification (QC): confirm the change is live.
                try:
                    live = wp.get_meta(item["kind"], item["id"])
                    verified = (live.get(key) or "").strip() == new_val.strip()
                except WordPressError:
                    verified = False

                db.add(FixRecord(
                    site_id=site_id, finding_id=finding.id, fix_key=fix_key,
                    doer="SEO Technical",
                    action_taken=f"Set {field.replace('_', ' ')} to: {new_val}",
                    page_ref=item["link"], field=field,
                    before_value=old_val, after_value=new_val,
                    method="auto-safe", lane="autonomous", applied=True,
                    verification_verdict="verified" if verified else "not_fixed",
                    verify_hint=f"Confirm {field} on page equals the new value",
                    status="done",
                ))
                finding.status = "closed" if verified else "reopened"
                db.commit()
                fixes_made += 1

        run.status = "completed"
        run.summary = (
            f"Scanned {pages_scanned} pages, applied {fixes_made} meta fix(es) "
            f"(cap {MAX_FIXES})."
        )
        db.add(RunLog(site_id=site_id, message=f"Meta-fix run #{run_id} completed — {run.summary}"))
        db.commit()
    except Exception as exc:  # never let the thread die silently
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.add(RunLog(site_id=site_id, message=f"Meta-fix run #{run_id} failed: {exc}"))
            db.commit()
    finally:
        db.close()


def start_metafix_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(
        target=run_metafix, args=(site_id, run_id, conn), daemon=True
    ).start()


MAX_DEDUPE = 10


def run_dedupe_titles(site_id: int, run_id: int, conn: dict) -> None:
    """Make duplicate page titles unique. Keeps one page per duplicated title and
    drafts a fresh, content-specific title for the rest, gated for approval."""
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        wp = WordPressClient(conn["url"], conn["username"], conn["app_password"])
        ok, code = wp.test()
        if not ok:
            run.status = "failed"
            run.summary = f"WordPress connection failed (HTTP {code})."
            db.commit()
            return

        items = wp.list_content(limit=60)
        groups: dict[str, list] = {}
        for it in items:
            t = (it.get("title") or "").strip().lower()
            if t:
                groups.setdefault(t, []).append(it)
        dups = {t: lst for t, lst in groups.items() if len(lst) > 1}
        if not dups:
            run.status = "completed"
            run.summary = "No duplicate titles found."
            db.commit()
            return

        open_findings = (
            db.query(Finding)
            .filter(Finding.site_id == site_id, Finding.category == "duplicate_title",
                    Finding.status == "open")
            .all()
        )
        rules = rules_for("shared", "seo_technical")
        drafted = 0
        for _t, lst in dups.items():
            if drafted >= MAX_DEDUPE:
                break
            orig_title = lst[0].get("title") or ""
            finding = next((f for f in open_findings if orig_title and orig_title[:40].lower() in (f.issue or "").lower()), None)
            # Keep lst[0] as-is; rewrite the others to be unique.
            for it in lst[1:]:
                if drafted >= MAX_DEDUPE:
                    break
                old_title = (it["meta"].get(YOAST_TITLE_KEY) or it.get("title") or "").strip()
                try:
                    s = generate_meta(it["title"], it["link"], it["content_text"],
                                      conn.get("site_name", ""), rules=rules)
                except Exception:
                    continue
                new_title = s.get("title", "")
                if not new_title or new_title.strip().lower() == old_title.lower():
                    continue
                db.add(Approval(
                    site_id=site_id, kind="meta_rewrite",
                    title=f"Make title unique: {it['link']}",
                    summary=f"Duplicate title. “{old_title}” → “{new_title}”.",
                    payload=json.dumps({
                        "finding_id": finding.id if finding else None,
                        "page_kind": it["kind"], "page_id": it["id"],
                        "new_title": new_title, "new_desc": "",
                        "old_title": old_title, "old_desc": "",
                    }),
                    status="pending",
                ))
                drafted += 1
            if finding:
                finding.status = "in-progress"
            db.commit()

        run.status = "completed"
        run.summary = (f"Drafted {drafted} unique title(s) — waiting for approval."
                       if drafted else "Duplicate titles found but no rewrite was needed.")
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_dedupe_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_dedupe_titles, args=(site_id, run_id, conn), daemon=True).start()
