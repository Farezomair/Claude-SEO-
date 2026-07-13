"""Fix Chat — talk to the site, get fixes applied.

A chat box scoped to ONE website: "the real phone is 208 856 3233, update it
everywhere" becomes executed, verified changes — without a Claude-Code session.

Scope is enforced twice:
1. The planner prompt refuses anything that isn't a fix to THIS site.
2. Architecturally, the only operation that exists is exact-text replacement on
   this site's own page bodies — there is nothing else it CAN do. No browsing,
   no other sites, no research.

Every applied change gets a SiteChange snapshot (revertible) and a FixRecord,
and the result is verified on the live homepage before the chat claims success.
"""
import json
import re
import threading

import httpx

from .brain import ANTHROPIC_MODEL, _extract_json, _get_client
from .database import SessionLocal
from .elementor_agent import AbilitiesClient, list_elementor_pages, read_body, write_body
from .models import FixRecord, JobRun, RunLog, Site, SiteChange

MAX_OPS = 12
MAX_PAGES = 40


def _site_facts(site_url: str) -> str:
    """Context for the planner: contact-ish strings actually on the live site
    (phones, tel:/mailto: links, license-like tokens, address lines) so it can
    map old -> new even when the owner only supplies the new value."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "SEO-Agent/1.0"}) as c:
            html = c.get(site_url).text
    except Exception:
        return "(couldn't read the live homepage)"
    found = set()
    for m in re.finditer(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", html):
        found.add(m.group(0))
    for m in re.finditer(r'href=["\'](tel:[^"\']+|mailto:[^"\']+)["\']', html, re.I):
        found.add(m.group(1))
    for m in re.finditer(r"(?:lic(?:ense|ence)?\.?|registration)\s*(?:no\.?|number|#)?\s*[:#]?\s*[A-Z]{0,5}-?\w{3,12}",
                         html, re.I):
        found.add(re.sub(r"\s+", " ", m.group(0)).strip())
    for m in re.finditer(r'"telephone"\s*:\s*"([^"]+)"|"streetAddress"\s*:\s*"([^"]+)"', html):
        found.add(m.group(1) or m.group(2))
    return "\n".join(sorted(x for x in found if x))[:2500] or "(none found)"


def plan_site_fixes(site_name: str, site_url: str, facts: str, queue: str,
                    message: str, history: list) -> dict:
    """Ask Claude to turn the owner's message into operations — or refuse."""
    convo = "\n".join(f"{m.get('role', 'user')}: {str(m.get('text', ''))[:300]}"
                      for m in (history or [])[-6:])
    prompt = f"""You are the Fix Chat of Ascend, operating on EXACTLY ONE website: {site_name} ({site_url}).

Your ONLY powers, all limited to this site:
1. replace — exact text replacement across the site's pages (contact details, license numbers, names, prices, wording; every format variant incl. tel:/mailto: links and JSON-LD values).
2. pattern — a regular-expression replacement/removal across the site's pages, for things like "remove the posted date everywhere" where the exact strings vary. Write a precise, conservative Python regex (no catastrophic backtracking); replacement may be an empty string to remove. Prefer anchoring on surrounding markup when it makes the pattern safer.
3. approve / reject — decide a PENDING approval from the queue below, by its exact id. approve may set publish true (live) or false (draft) when the owner says so; default publish true for fixes they explicitly asked to go live.
4. resolve_task — mark a "needs your attention" task from the list below as done, ONLY when the owner's message actually supplies or fixes what it asked for.

STRICT SCOPE — refuse everything else with ONE friendly sentence and zero operations:
- No research, no shopping/comparisons, no other websites, no code, no general questions.
- Never invent facts; if a fix needs a value the owner didn't give, ask for it (zero operations).
- approve/reject/resolve_task may ONLY use ids from the queue below.

Strings currently found on the live site (locate OLD values and their variants here):
\"\"\"{facts}\"\"\"

Pending approvals & owner tasks on this site:
\"\"\"{queue}\"\"\"

{f'Recent conversation:{chr(10)}{convo}' if convo else ''}

The owner says:
\"\"\"{message[:800]}\"\"\"

Respond with ONLY a JSON object:
{{"reply": "1-3 friendly sentences: what you're doing (or why not / what you still need)",
  "operations": [
    {{"op": "replace", "find": "...", "replace": "..."}},
    {{"op": "pattern", "regex": "...", "replace": ""}},
    {{"op": "approve", "id": 12, "publish": true}},
    {{"op": "reject", "id": 13}},
    {{"op": "resolve_task", "id": 44}}
  ]}}
(operations may be empty; max {MAX_OPS} total)"""
    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=1800,
        system="You execute website fixes for one specific site and refuse everything else. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json(next((b.text for b in response.content if b.type == "text"), ""))
    ops = []
    for r in (data.get("operations") or [])[:MAX_OPS]:
        op = str(r.get("op", "")).strip()
        if op == "replace":
            find, rep = str(r.get("find", "")), str(r.get("replace", ""))
            if find and find != rep and len(find) >= 4:
                ops.append({"op": "replace", "find": find, "replace": rep})
        elif op == "pattern":
            rx = str(r.get("regex", ""))
            if 4 <= len(rx) <= 200:
                try:
                    re.compile(rx)
                    ops.append({"op": "pattern", "regex": rx, "replace": str(r.get("replace", ""))})
                except re.error:
                    pass
        elif op in ("approve", "reject", "resolve_task"):
            try:
                ops.append({"op": op, "id": int(r.get("id")),
                            "publish": bool(r.get("publish", True))})
            except (TypeError, ValueError):
                pass
    return {"reply": str(data.get("reply", "")).strip()[:600] or "Done.", "operations": ops}


def run_chat_fix(site_id: int, run_id: int, conn: dict, message: str, history: list) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)

        def _label(t):
            run.progress_label = t[:290]
            db.commit()

        _label("Reading the site and planning the fix…")
        facts = _site_facts(site.url)

        # Queue context: this site's pending approvals + owner tasks (with ids —
        # the ONLY ids approve/reject/resolve_task are allowed to touch).
        from .models import Approval, Finding
        pend = (db.query(Approval).filter(Approval.site_id == site_id,
                                          Approval.status == "pending").all())
        tasks = (db.query(Finding).filter(Finding.site_id == site_id,
                                          Finding.status == "needs-human").all())
        qlines = [f"APPROVAL id={a.id} [{a.kind}] {a.title[:90]}" for a in pend[:20]]
        qlines += [f"TASK id={f.id} {(f.issue or '')[:110]}" for f in tasks[:20]]
        queue = "\n".join(qlines) or "(queue is empty)"
        valid_appr = {a.id for a in pend}
        valid_task = {f.id for f in tasks}

        try:
            plan = plan_site_fixes(site.name, site.url, facts, queue, message, history)
        except Exception as exc:
            run.status = "failed"
            run.summary = f"I couldn't plan that ({exc.__class__.__name__}) — try rephrasing."
            db.commit()
            return

        ops = plan["operations"]
        if not ops:
            run.status = "completed"
            run.summary = plan["reply"]
            db.commit()
            return

        results = []

        # --- queue operations (same code path as the approval buttons) ---
        from .approval_actions import apply_approval, reject_approval
        for o in [o for o in ops if o["op"] in ("approve", "reject", "resolve_task")]:
            if o["op"] == "resolve_task":
                if o["id"] not in valid_task:
                    results.append(f"task #{o['id']}: not in this site's queue — skipped")
                    continue
                f = db.get(Finding, o["id"])
                f.status = "closed"
                f.remark = "Resolved via Fix Chat (owner supplied/fixed the real data)."
                db.commit()
                results.append(f"task #{o['id']}: marked done")
            else:
                if o["id"] not in valid_appr:
                    results.append(f"approval #{o['id']}: not pending on this site — skipped")
                    continue
                appr = db.get(Approval, o["id"])
                _label(f"{'Approving' if o['op'] == 'approve' else 'Rejecting'}: {appr.title[:60]}…")
                if o["op"] == "approve":
                    ok, notice = apply_approval(db, appr, o.get("publish", True))
                    results.append(f"“{appr.title[:60]}”: {'approved & applied' if ok else 'failed (' + notice + ')'}")
                else:
                    reject_approval(db, appr)
                    results.append(f"“{appr.title[:60]}”: rejected")

        # --- text/pattern operations across page bodies ---
        text_ops = [o for o in ops if o["op"] in ("replace", "pattern")]
        if text_ops:
            client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
            pages = list_elementor_pages(conn)[:MAX_PAGES]
            pages_changed = total_hits = 0
            for i, p in enumerate(pages, start=1):
                pid = p.get("id")
                if not pid:
                    continue
                _label(f"Applying across the site… page {i} of {len(pages)}")
                body = read_body(client, pid)
                if not body:
                    continue
                new_body, hits = body, 0
                for o in text_ops:
                    if o["op"] == "replace":
                        n = new_body.count(o["find"])
                        if n:
                            hits += n
                            new_body = new_body.replace(o["find"], o["replace"])
                    else:
                        new_body, n = re.subn(o["regex"], o["replace"], new_body)
                        hits += n
                if hits > 300:
                    results.append(f"page {p.get('title', pid)}: {hits} matches looked unsafe — skipped")
                    continue
                if hits and new_body != body and write_body(client, pid, new_body):
                    pages_changed += 1
                    total_hits += hits
                    db.add(SiteChange(
                        site_id=site_id, kind="chat_fix",
                        request=f"Fix Chat: {message[:120]}",
                        css=new_body, old_css=body, status="applied",
                        target_page_id=pid, target_widget_id=""))
                    db.commit()
            if total_hits:
                results.append(f"{total_hits} text change(s) across {pages_changed} page(s)")
                db.add(FixRecord(
                    site_id=site_id, doer="Fix Chat", field="chat_fix",
                    action_taken=f"Chat fix: {message[:160]}",
                    page_ref=site.url, before_value=f"{len(text_ops)} operation(s)",
                    after_value=f"{total_hits} occurrence(s) across {pages_changed} page(s)",
                    method="chat", lane="gated", applied=True,
                    verification_verdict="applied", status="done"))
            else:
                results.append("no matching text found on any page")

        run.status = "completed"
        run.summary = (plan["reply"] + " — " + "; ".join(results)
                       + ". The next audit re-checks everything.") if results else plan["reply"]
        db.add(RunLog(site_id=site_id, message=f"Fix Chat: {run.summary[:200]}"))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"That fix failed ({exc.__class__.__name__}) — nothing was left half-done; try again."
            db.commit()
    finally:
        db.close()


def start_chat_fix_async(site_id: int, run_id: int, conn: dict, message: str, history: list) -> None:
    threading.Thread(target=run_chat_fix, args=(site_id, run_id, conn, message, history), daemon=True).start()
