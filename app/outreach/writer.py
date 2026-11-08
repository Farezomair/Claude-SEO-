"""Research a prospect (site + live Google checks), write a tailored first email, have an 'owner' score it.

Business specifics (sender persona, company, example emails) come from the stored settings,
not from this code.
"""
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from . import config, db, llm, rules, serp, site_reader, templates

DECISION_ROLES = r"owner|founder|co-founder|ceo|chief executive|president|executive director|director of marketing|marketing director|marketing manager|administrator|program director|clinical director|chief operating|coo|managing director|director of business development|admissions director|director of admissions"


class Email1Fields(BaseModel):
    fit: Literal["fit", "skip"] = Field(description="skip if a hospital, large chain or national brand, government clinic, closed, not addiction/behavioral health, or the site credits an SEO, marketing or web agency")
    skip_reason: str = Field(description="why skipped; empty if fit")
    brand_name: str = Field(description="the center's name exactly as their own website shows it to families, not a legal or LLC name")
    first_name: str = Field(description="first name of the owner/CEO/executive director/admissions, business development or marketing lead, only if the site names them with that role (page text or a staff photo file name); else empty")
    first_name_role: str = Field(description="their role exactly as the site states it; empty if no name")
    first_name_quote: str = Field(description="short exact text copied from the research that shows the name and role together; empty if no name")
    subject: str = Field(description="2 to 7 words that state the finding, like a colleague's note. No 'free', no question or exclamation marks, no clickbait")
    opening: str = Field(description="paragraph 1, 40 to 65 words: the live Google result (search in quotes, city, where they landed, who took the top Maps spots using short names, at most two or three) plus, if it fits, the site detail that explains it")
    bridge: str = Field(description="paragraph 2, one or two sentences, max 30 words: the most useful provable reason or fix, framed helpfully, not as a mistake. Empty if it would be filler")
    observation_basis: Literal["present", "absent", "none"] = Field(description="present = a site detail backed by quotes; absent = something missing from the site; none = the email uses only search results")
    evidence_quotes: list[str] = Field(description="one short exact text copied from their site for EACH site fact the email states; else empty")
    absent_terms: list[str] = Field(description="if absent: 2 to 4 lowercase words that would appear in their URLs, menu or text if the missing thing existed; else empty")
    search_facts_used: list[str] = Field(description="every business name (as written in the email, e.g. 'Second Chances'), search phrase and position (e.g. '#14') from the live Google checks that the email mentions. Only the words themselves, never field names")
    area: str = Field(description="the city or area families search from, e.g. 'Lafayette', 'Monroe or Ruston'. Max 4 words")
    service_phrase: str = Field(description="what those families search for, fits after 'search for', e.g. 'detox or an IOP', 'rehab', 'men's rehab'. Only services they offer. Max 5 words, no place name")
    fu1_line: str = Field(description="one sentence for the day-4 follow-up, max 25 words, everyday words: the one change you'd make first and what it does for a family")


class OwnerReview(BaseModel):
    score: int = Field(description="1 to 10: how likely you, as this center's owner, would reply 'yes, send it'")
    believable: bool = Field(description="does it read like a real person who actually looked at our site and searches, not a mass email or AI")
    fixes: list[str] = Field(description="specific changes that would raise the score; empty if 9 or 10")


SYSTEM = """You write the first cold email from {sender} at {company} to a US addiction treatment or behavioral health center.

Who reads it: the owner, executive director, admissions or business development lead. Busy, skeptical, gets agency spam every week, not a marketer. They reply to an email that (1) proves a real person checked THEIR searches and site, (2) tells them something useful they didn't know, and (3) offers something specific and free that is easy to say yes to.

You fill the variable parts. The template adds a fixed offer paragraph after yours: "I do local search for treatment centers. If it's useful, I'll send you a free one-page breakdown: what a family in <area> sees when they search for <service_phrase>, where <center> shows up, and the first three things I'd fix. By email within two days, no call needed. Want it?" So do not introduce us or make an offer yourself.

How the opening works (this is what gets replies):
- Lead with one real live Google result: the search in quotes, that you ran it not logged in from a <city> location, exactly where the center landed (the position the data gives, e.g. "#14, on page two", or "not in the top 20"), and who took the top Google Maps spots. Use short business names (e.g. "Second Chances", not "Second Chances Addiction Recovery Center") and at most two or three names in total.
- Prefer a local search (city name, "near me" style) over a statewide one. Use one search, two at most.
- The whole email above the signature must stay under 140 words.
- Pick the search that matters most for them. If a page of theirs ranks well but has a problem (for example it ranks for a service that page says they don't offer there), that is the strongest point.
- Then one provable "why" or "what to do" from their site: a city they serve that the site never mentions, a page that leaves a family without a next step, sample text left on a page, nothing about insurance.
- Frame problems helpfully, never as their mistake. No scare lines.

Approved examples (match this quality and tone, never copy their facts):
{examples}

Rules:
- Use only facts from the check data and the site research. Copy every name, search phrase and position you use into search_facts_used, and every site fact into evidence_quotes.
- Say "this week" only if the check date is within 7 days of today ({today}).
- Never say a ranking position the data doesn't show. If the center isn't in the data, say it wasn't on page one.
- Everyday words. Never: SEO, H1, meta, keyword, rank, ranking, landing page, template text, conversion, URL, optimize.
- Never claim anything about how a page looks or works (headings, phone links, buttons, forms, speed).
- No URLs or domains, no em dashes or en dashes, no hyphen used as a dash, no hype, no fake urgency, no flattery, no guarantees, nothing about patients, outcomes or medical claims, no claims of past results, no invented details (drive times, distances, dates).
- Write like a person: no "I hope this finds you well", "I wanted to reach out", "seamless", "elevate", "unlock", "journey".

First name: only if the research clearly names the owner, founder, CEO, executive director, administrator, admissions, business development or marketing lead (page text, or a staff photo file name like Jane_Doe-Executive_Director). Copy the exact text into first_name_quote. If unsure, leave it empty. Never guess.

Mark skip when the center is a hospital, a large chain or national brand (marketing run centrally), a government clinic, closed, not a treatment provider, or credits an SEO, marketing or web agency anywhere on the site."""

REVIEWER = """You are the owner of a small addiction treatment center. You get several cold emails a week from marketing agencies and ignore almost all of them. Read this cold email as you really would, and judge how likely you are to reply "yes, send it".

You reply when the email clearly shows someone checked YOUR searches and site, tells you something specific and useful, sounds like a real person, is short, and the free offer is concrete and easy to accept. You ignore anything generic, salesy, long, vague, AI-sounding, or that feels like a trick.

Fixed constraints you must accept and not ask to change: no links in a first email apart from the sender's own domain in the signature, no claims of past clients or case studies (none yet), the company name, the sign-off, the mailing address and the opt-out line are required.

Score 1 to 10 and list specific fixes. Be strict: 8 means you'd probably reply."""


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip().strip('"').strip("'")


def verify(fields: Email1Fields, site: dict, listing: dict, checks: list) -> tuple[list[str], str]:
    problems, name = [], "there"
    corpus = site_reader.corpus(site)
    if fields.first_name:
        q = _norm(fields.first_name_quote).replace("_", " ").replace("-", " ")
        corp = corpus.replace("_", " ").replace("-", " ")
        if q and q in corp and fields.first_name.lower() in q and re.search(DECISION_ROLES, q + " " + fields.first_name_role.lower()):
            name = fields.first_name.strip().split()[0].capitalize()
    if not fields.brand_name or _norm(fields.brand_name) not in corpus:
        problems.append(f"brand name {fields.brand_name!r} not found on their site")
    if re.search(r"\b(llc|inc|l\.l\.c)\b", fields.brand_name.lower()):
        problems.append("brand name contains LLC/Inc")
    for q in fields.evidence_quotes:
        if q.strip() and _norm(q) not in corpus:
            problems.append(f"quote not found on their site: {q[:60]!r}")
    if fields.observation_basis == "present" and not [q for q in fields.evidence_quotes if q.strip()]:
        problems.append("site observation has no supporting quotes")
    if fields.observation_basis == "absent":
        terms = [t.lower().strip() for t in fields.absent_terms if t.strip()]
        if not terms:
            problems.append("absence claim without check terms")
        hits = [t for t in terms if t in corpus]
        if hits:
            problems.append(f"absence claim contradicted: found {hits} on their site")
        if not site.get("all_urls"):
            problems.append("no sitemap: absence claim could be wrong")
    serp_text = json.dumps(checks).lower()
    for f in fields.search_facts_used:
        if f.strip() and _norm(f) not in serp_text:
            problems.append(f"search fact not in the live checks: {f[:60]!r}")
    if checks and not fields.search_facts_used:
        problems.append("live Google checks exist but the email does not use one")
    places = corpus + " " + (listing.get("cities") or "").lower() + " louisiana ohio " + serp_text
    for word in re.findall(r"[A-Z][a-z]+", fields.area):
        if word.lower() not in places and word.lower() not in ("north", "south", "east", "west", "central", "greater", "or", "and"):
            problems.append(f"area {fields.area!r} not found in the research")
            break
    if len(fields.opening.split()) > 90:
        problems.append("opening too long, keep it under 80 words")
    if len(fields.bridge.split()) > 40:
        problems.append("bridge too long")
    if len(fields.subject.split()) > 8 or re.search(r"free|!|\?", fields.subject, re.I):
        problems.append("subject must be 2 to 7 words, no 'free', no ! or ?")
    j = rules.jargon_in(" ".join([fields.subject, fields.opening, fields.bridge, fields.fu1_line]))
    if j:
        problems.append(f"jargon an owner wouldn't use: {j}")
    return problems, name


def research(prospect_id: int, refresh=False) -> dict:
    with db.connect() as con:
        row = con.execute("SELECT * FROM research WHERE prospect_id=?", (prospect_id,)).fetchone()
        if row and not refresh:
            return json.loads(row["data"])
        p = con.execute("SELECT website FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    site = site_reader.read_site(p["website"])
    with db.connect() as con:
        con.execute("DELETE FROM research WHERE prospect_id=?", (prospect_id,))
        con.execute("INSERT INTO research(prospect_id,fetched_at,data) VALUES(?,?,?)",
                    (prospect_id, db.now_iso(), json.dumps(site)))
    return site


def _render(cfg, fields: Email1Fields, name: str, center: str, prospect_id: int):
    slots = templates.base_slots(cfg)
    opening = fields.opening.strip()
    if fields.bridge.strip():
        opening += "\n\n" + fields.bridge.strip()
    slots.update(name=name, center=center, subject=fields.subject.strip(), opening=opening,
                 area=fields.area.strip(), service_phrase=fields.service_phrase.strip().rstrip("."),
                 offer_index=prospect_id)
    return templates.render("email1", **slots), slots


def draft_email1(prospect_id: int, max_rounds: int = 4) -> dict:
    cfg = config.load()
    with db.connect() as con:
        p = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    site = research(prospect_id)
    if not site.get("reachable"):
        with db.connect() as con:
            db.set_status(con, prospect_id, "skipped", note="site unreachable: " + "; ".join(site.get("errors", [])))
        return {"status": "skipped", "reason": "site unreachable"}
    if re.search(r"scaled\s*ai\b|scaledai", site_reader.corpus(site)):
        with db.connect() as con:
            db.set_status(con, prospect_id, "skipped", note="site mentions Scaled AI")
        return {"status": "skipped", "reason": "site mentions Scaled AI"}

    checks = serp.recent(prospect_id)
    if not checks:
        home = (site.get("pages") or [{}])[0]
        summary = " ".join([home.get("title", ""), home.get("meta_description", ""), home.get("text", "")[:2000]])
        serp.check_prospect(p, summary, p["center"])
        checks = serp.recent(prospect_id)

    listing = {k: p[k] for k in ("center", "website", "state", "cities", "levels_of_care", "phone")}
    system = SYSTEM.format(sender=cfg["sender"]["name"] or "the sender", company=cfg["company"]["name"] or "our agency",
                           examples=cfg.get("writer_examples") or "(none yet)", today=db.now_iso()[:10])
    user = ("SAMHSA listing for this center:\n" + llm.as_json(listing) +
            ("\n\nlive_google_checks (location set to the city, not logged in):\n" + llm.as_json(checks) if checks else
             "\n\n(no live Google checks available: write from the site only)") +
            "\n\nWhat we read on their website:\n" + llm.as_json(site_reader.for_prompt(site)))

    feedback, fields, problems, name, review = "", None, [], "there", None
    subject = body = ""
    min_score = int(cfg.get("auto_send_min_score") or 8)
    for _ in range(max_rounds):
        fields = llm.structured(system, user + feedback, Email1Fields, effort="high")
        if fields.fit == "skip":
            with db.connect() as con:
                db.set_status(con, prospect_id, "skipped", note="not a fit: " + fields.skip_reason)
            return {"status": "skipped", "reason": fields.skip_reason}
        problems, name = verify(fields, site, listing, checks)
        center = (fields.brand_name or p["center"]).strip()
        (subject, body), slots = _render(cfg, fields, name, center, prospect_id)
        problems += rules.check("email1", subject, body, slots)
        if problems:
            feedback = "\n\nYour previous answer failed these checks, fix them: " + "; ".join(problems)
            continue
        review = llm.structured(REVIEWER, f"Subject: {subject}\n\n{body}", OwnerReview, effort="medium", max_tokens=4000)
        if review.score >= min_score and review.believable:
            break
        problems = [f"owner score {review.score}/10: " + "; ".join(review.fixes)]
        feedback = (f"\n\nA center owner read your last draft and scored it {review.score}/10. Their notes: "
                    + "; ".join(review.fixes) + "\nRewrite to fix what you can while keeping every fact verifiable "
                    "and inside the rules (ignore any note that asks for links, invented numbers or removing the signature).")

    passed = not problems
    status = "approved" if passed else "needs_review"
    notes = {"problems": problems, "fields": fields.model_dump(), "review": review.model_dump() if review else None,
             "auto_approved": passed}
    with db.connect() as con:
        con.execute("DELETE FROM messages WHERE prospect_id=? AND kind='email1' AND status IN ('draft','needs_review','approved')", (prospect_id,))
        con.execute("""INSERT INTO messages(prospect_id,kind,direction,status,to_addr,from_addr,subject,body,review_notes,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (prospect_id, "email1", "out", status, p["email"], cfg["sender"]["email"], subject, body,
                     json.dumps(notes), db.now_iso()))
        con.execute("UPDATE prospects SET first_name=?, first_name_source=? WHERE id=?",
                    (None if name == "there" else name, fields.first_name_quote if name != "there" else None, prospect_id))
        if fields.brand_name and passed:
            con.execute("UPDATE prospects SET center=? WHERE id=?", (fields.brand_name.strip(), prospect_id))
        db.set_status(con, prospect_id, "approved" if passed else "needs_review")
    return {"status": status, "problems": problems, "subject": subject, "body": body,
            "score": review.score if review else None}
