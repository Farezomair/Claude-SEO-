"""The Strategy Brain — the app's understanding of the business.

An editable questionnaire the owner fills in. Every generative/judgemental part of
the app reads the compiled context so it works WITH the owner's strategy instead of
against it: it stops re-flagging things the owner has declared intentional, aligns
generated copy and audits to the real business model and maturity, and steers
sensitive items (e.g. reviews on a brand-new business) toward the owner's chosen
honest approach rather than nagging.
"""
import json

from .models import BusinessBrain

# The questionnaire. Driven from one place so the form, storage, and prompt
# context all stay in sync. type: text | textarea | select | multi.
BRAIN_QUESTIONS = [
    {"key": "stage", "type": "select",
     "q": "How mature is the business right now?",
     "help": "So the app knows whether claims (reviews, project counts, years) can be backed up yet.",
     "options": ["Brand new — no real reviews or track record yet",
                 "Growing — a few real customers, still light on proof",
                 "Established — real reviews and a real portfolio exist"]},
    {"key": "models", "type": "multi",
     "q": "Which revenue models are ACTIVE on this site today?",
     "help": "The auditors judge the site against what's actually active, not a generic template.",
     "options": ["Lead generation (capture leads, route to installers/pros)",
                 "Dropshipping store", "Affiliate / product roundups",
                 "AdSense / display blog", "Rank & rent",
                 "Direct service (we do the work ourselves)"]},
    {"key": "fulfillment", "type": "text",
     "q": "Who actually delivers the service to the customer?",
     "placeholder": "e.g. We route leads to vetted local outdoor-kitchen installers"},
    {"key": "positioning", "type": "select",
     "q": "How should the site honestly present itself?",
     "help": "The content generator and both auditors align to this so nothing has to be faked.",
     "options": ["A connector that matches homeowners with vetted local pros",
                 "A direct provider that does the work itself",
                 "A content / affiliate site"]},
    {"key": "trust_stance", "type": "select",
     "q": "Until real reviews exist, how should reviews & testimonials be handled?",
     "help": "This stops the app re-nagging and tells the doers the right fix. Fabricated reviews are an FTC + Google-penalty risk, so the options are the honest ones.",
     "options": ["Remove them until we have real ones",
                 "Reframe as a connector so we don't need our own reviews",
                 "Only show verified reviews from a real source (Google/GBP)"]},
    {"key": "do_not_flag", "type": "textarea",
     "q": "Anything the app should treat as INTENTIONAL and stop flagging?",
     "placeholder": "e.g. The blog is thin on purpose for now; we're building it out over the next quarter."},
    {"key": "priorities", "type": "textarea",
     "q": "What matters most in the next 90 days?",
     "placeholder": "e.g. Capture more free-estimate leads from Meridian and nearby Treasure Valley areas."},
    {"key": "differentiators", "type": "textarea",
     "q": "What genuinely sets you apart? (only real things)",
     "placeholder": "e.g. Fast local matching, no-obligation quotes, licensed installers only."},
    {"key": "constraints", "type": "textarea",
     "q": "Any constraints or hard rules the app must respect?",
     "placeholder": "e.g. Never claim a physical showroom; never invent prices."},
]

_LABELS = {q["key"]: q["q"] for q in BRAIN_QUESTIONS}


def get_brain(db, site_id: int):
    return db.query(BusinessBrain).filter(BusinessBrain.site_id == site_id).first()


def answers_of(brain) -> dict:
    if not brain or not (brain.answers_json or "").strip():
        return {}
    try:
        d = json.loads(brain.answers_json)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_brain(db, site_id: int, answers: dict) -> None:
    brain = get_brain(db, site_id)
    if not brain:
        brain = BusinessBrain(site_id=site_id)
        db.add(brain)
    clean = {}
    for q in BRAIN_QUESTIONS:
        v = answers.get(q["key"])
        if isinstance(v, list):
            clean[q["key"]] = [str(x)[:200] for x in v][:12]
        elif v not in (None, ""):
            clean[q["key"]] = str(v)[:800]
    brain.answers_json = json.dumps(clean)
    db.commit()


def is_configured(brain) -> bool:
    return bool(answers_of(brain))


def brain_context(db, site_id: int) -> str:
    """Compile the owner's answers into a prompt block that every AI part reads,
    so the app works WITH the strategy. Empty string when not filled in yet."""
    a = answers_of(get_brain(db, site_id))
    if not a:
        return ""
    lines = []
    for q in BRAIN_QUESTIONS:
        v = a.get(q["key"])
        if not v:
            continue
        val = ", ".join(v) if isinstance(v, list) else v
        lines.append(f"- {q['q']} {val}")
    if not lines:
        return ""
    return ("\n\nBUSINESS STRATEGY (owner-declared — work WITH this, do not contradict it):\n"
            + "\n".join(lines)
            + "\nUse this to judge and write appropriately: do NOT flag as problems the things the "
            "owner has declared intentional, align copy and recommendations to the stated positioning "
            "and maturity, and for anything unverifiable (reviews, counts) follow the owner's stated "
            "stance rather than inventing or nagging. Never fabricate to fill a gap.\n")


def brain_context_str(site_id: int) -> str:
    from .database import SessionLocal
    db = SessionLocal()
    try:
        return brain_context(db, site_id)
    finally:
        db.close()
