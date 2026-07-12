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


def plan_site_fixes(site_name: str, site_url: str, facts: str, message: str, history: list) -> dict:
    """Ask Claude to turn the owner's message into exact replacements — or refuse."""
    convo = "\n".join(f"{m.get('role', 'user')}: {str(m.get('text', ''))[:300]}"
                      for m in (history or [])[-6:])
    prompt = f"""You are the Fix Chat of Ascend, operating on EXACTLY ONE website: {site_name} ({site_url}).

Your ONLY power is exact text replacement across this site's own pages (content, links, schema). You use it to execute the owner's fixes: correcting phone numbers, license/registration numbers, addresses, business names, prices, opening hours, wrong wording — anywhere they appear, in every format variant (visible text, tel:/mailto: links, JSON-LD schema values).

STRICT SCOPE — refuse everything else with ONE friendly sentence and zero operations:
- No research, no shopping/comparisons, no questions about other websites or the world, no code, no opinions.
- No inventing facts: only apply values the owner explicitly gave. If the fix needs a value they didn't provide, ask for it (zero operations).

Strings currently found on the live site (use these to locate the OLD values and their format variants):
\"\"\"{facts}\"\"\"

{f'Recent conversation:{chr(10)}{convo}' if convo else ''}

The owner says:
\"\"\"{message[:800]}\"\"\"

If this is an in-scope fix: produce the complete list of exact replacements — every format variant that exists on the site (e.g. "(208) 555-0123", "208-555-0123", "tel:+12085550123", schema "telephone" values; keep each replacement's format consistent with what it replaces). Max {MAX_OPS}.

Respond with ONLY a JSON object:
{{"reply": "1-3 friendly sentences: what you're changing (or why you can't / what you still need)",
  "replacements": [{{"find": "exact old text", "replace": "exact new text"}}]}}"""
    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=1500,
        system="You execute website text fixes for one specific site and refuse everything else. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    data = _extract_json(next((b.text for b in response.content if b.type == "text"), ""))
    reps = []
    for r in (data.get("replacements") or [])[:MAX_OPS]:
        find, rep = str(r.get("find", "")), str(r.get("replace", ""))
        if find and rep and find != rep and len(find) >= 4:
            reps.append({"find": find, "replace": rep})
    return {"reply": str(data.get("reply", "")).strip()[:600] or "Done.", "replacements": reps}


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
        try:
            plan = plan_site_fixes(site.name, site.url, facts, message, history)
        except Exception as exc:
            run.status = "failed"
            run.summary = f"I couldn't plan that ({exc.__class__.__name__}) — try rephrasing."
            db.commit()
            return

        reps = plan["replacements"]
        if not reps:
            run.status = "completed"
            run.summary = plan["reply"]
            db.commit()
            return

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
            for r in reps:
                n = new_body.count(r["find"])
                if n:
                    hits += n
                    new_body = new_body.replace(r["find"], r["replace"])
            if hits and new_body != body and write_body(client, pid, new_body):
                pages_changed += 1
                total_hits += hits
                db.add(SiteChange(
                    site_id=site_id, kind="chat_fix",
                    request=f"Fix Chat: {message[:120]}",
                    css=new_body, old_css=body, status="applied",
                    target_page_id=pid, target_widget_id=""))
                db.commit()

        # Verify on the live homepage: new values present / old ones gone.
        _label("Verifying on the live site…")
        verified = ""
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"}) as c:
                live = c.get(site.url).text
            gone = all(r["find"] not in live for r in reps)
            present = any(r["replace"] in live for r in reps)
            if gone and present:
                verified = " Verified on the live homepage."
            elif not gone:
                verified = " Note: the old text may still show on cached pages for a few minutes."
        except Exception:
            pass

        if total_hits:
            db.add(FixRecord(
                site_id=site_id, doer="Fix Chat", field="chat_fix",
                action_taken=f"Chat fix: {'; '.join(r['find'][:30] + ' → ' + r['replace'][:30] for r in reps[:4])}",
                page_ref=site.url, before_value=f"{len(reps)} replacement rule(s)",
                after_value=f"{total_hits} occurrence(s) across {pages_changed} page(s)",
                method="chat", lane="gated", applied=True,
                verification_verdict="verified" if "Verified" in verified else "applied",
                status="done"))
            run.summary = (f"{plan['reply']} Applied {total_hits} change(s) across {pages_changed} page(s)."
                           + verified + " The next audit will re-check everything.")
        else:
            run.summary = (plan["reply"] + " I couldn't find that text on any page though — "
                           "double-check the exact old value?")
        run.status = "completed"
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
