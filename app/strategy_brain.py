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
    {"key": "category", "type": "select",
     "q": "What kind of website / business is this?",
     "help": "So the auditors judge it by the right yardstick — a store, a SaaS, a blog and a local service each need different things.",
     "options": ["E-commerce / online store", "Dropshipping store",
                 "SaaS / software product", "Local service business",
                 "Lead generation site", "Marketplace / platform",
                 "Content / blog / publisher (ad-supported)", "Affiliate site",
                 "Agency / professional services", "Nonprofit / community",
                 "Personal brand / portfolio", "Other (describe in notes)"]},
    {"key": "stage", "type": "select",
     "q": "How mature is the business right now?",
     "help": "So the app knows whether claims (reviews, sales counts, years, case studies) can be backed up yet.",
     "options": ["Brand new — no real reviews or track record yet",
                 "Growing — some real customers, still light on proof",
                 "Established — real reviews / results / track record exist"]},
    {"key": "models", "type": "multi",
     "q": "How does the site make money? (tick all that apply)",
     "help": "The auditors judge the site against what's actually active, not a generic template.",
     "options": ["Selling our own products (e-commerce)", "Dropshipping",
                 "Subscription / SaaS", "Services / consulting (we do the work)",
                 "Bookings / appointments", "Lead generation (capture & route/sell leads)",
                 "Affiliate / referral commissions", "Advertising / display (AdSense etc.)",
                 "Marketplace / platform commission", "Donations / memberships"]},
    {"key": "primary_action", "type": "select",
     "q": "What is the ONE action you most want a visitor to take?",
     "help": "The conversion-path scoring is judged against this on every business type.",
     "options": ["Buy a product", "Start a subscription / free trial", "Book or schedule",
                 "Submit an enquiry / lead", "Call or contact us", "Sign up / create an account",
                 "Subscribe to email", "Donate", "Read / engage with content"]},
    {"key": "fulfillment", "type": "text",
     "q": "Who actually delivers the product or service to the customer?",
     "placeholder": "e.g. we ship it ourselves · a supplier dropships · we route leads to local providers · it's a digital product · third-party pros fulfil"},
    {"key": "positioning", "type": "select",
     "q": "How should the site honestly present itself?",
     "help": "The content writer and both auditors align to this so nothing has to be faked.",
     "options": ["We deliver the product/service ourselves directly",
                 "We connect customers with third-party providers (marketplace/connector)",
                 "We're a content / media / affiliate site",
                 "We're a software / SaaS product",
                 "Other (describe in notes)"]},
    {"key": "trust_stance", "type": "select",
     "q": "Until you have real reviews/results, how should reviews & testimonials be handled?",
     "help": "Stops the app re-nagging and tells the doers the right fix. Fabricated reviews are an FTC + Google-penalty risk, so the options are the honest ones.",
     "options": ["Remove them until we have real ones",
                 "Only show verified reviews from a real source (Google, Trustpilot, etc.)",
                 "Don't use our own reviews — rely on other trust signals",
                 "We already have real, verifiable reviews"]},
    {"key": "do_not_flag", "type": "textarea",
     "q": "Anything the app should treat as INTENTIONAL and stop flagging?",
     "placeholder": "e.g. the blog is thin on purpose for now · we don't want a phone number shown · pricing is deliberately hidden until a demo"},
    {"key": "priorities", "type": "textarea",
     "q": "What matters most for this site in the next 90 days?",
     "placeholder": "e.g. more product sales · more trial signups · more qualified leads · grow organic traffic to the blog"},
    {"key": "differentiators", "type": "textarea",
     "q": "What genuinely sets you apart? (only real things)",
     "placeholder": "e.g. faster shipping · a feature competitors lack · lower price · deeper expertise"},
    {"key": "constraints", "type": "textarea",
     "q": "Any constraints or hard rules the app must respect?",
     "placeholder": "e.g. never invent prices · never claim a physical location · keep the brand voice formal · don't mention competitors by name"},
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
