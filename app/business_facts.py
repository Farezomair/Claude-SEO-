"""Verified business facts — the single source of truth the generator may state.

Defect #2/#3/#5/#9 in the rebuild log all reduce to one rule: the content agent
must only assert real-world facts (NAP, credentials, ratings, prices) that come
from a verified source, and must never invent a plausible placeholder. This
module holds those verified values per site and turns them into (a) a prompt
block listing exactly what the generator is allowed to use, and (b) the set of
allowed values the trust gate checks generated copy against.
"""
import json

from .database import SessionLocal
from .models import BusinessFacts


def get_facts(db, site_id: int) -> BusinessFacts | None:
    return db.query(BusinessFacts).filter(BusinessFacts.site_id == site_id).first()


def pricing_rows(bf: BusinessFacts | None) -> list[dict]:
    if not bf or not (bf.pricing_json or "").strip():
        return []
    try:
        rows = json.loads(bf.pricing_json)
        return [{"item": str(r.get("item", "")).strip(), "price": str(r.get("price", "")).strip()}
                for r in rows if str(r.get("item", "")).strip()][:60]
    except Exception:
        return []


def facts_block(bf: BusinessFacts | None) -> str:
    """The prompt block: verified facts the generator MAY state, and an explicit
    note that anything absent must be omitted (never invented)."""
    if not bf:
        return ("\n\nVERIFIED BUSINESS FACTS: (none provided yet)\n"
                "You have NO verified facts for this business. Do not state any phone number, "
                "license number, street address, rating, review count, project count, years in "
                "business, or specific price. Omit them entirely — never invent a placeholder.\n")
    lines = []
    if bf.phone:
        lines.append(f"- Phone: {bf.phone}")
    if bf.email:
        lines.append(f"- Email: {bf.email}")
    if bf.license_no:
        lines.append(f"- License/registration: {bf.license_no}")
    addr = ", ".join(p for p in (bf.street, bf.city, bf.region, bf.postal) if p)
    if bf.street and addr:
        lines.append(f"- Street address: {addr}")
    elif bf.city or bf.service_area:
        lines.append(f"- Service-area business (NO public street address). Serves: "
                     f"{bf.service_area or ', '.join(p for p in (bf.city, bf.region) if p)}")
    if bf.founded_year:
        lines.append(f"- Founded / in business since: {bf.founded_year}")
    if bf.rating and bf.review_count:
        lines.append(f"- Verified rating: {bf.rating} from {bf.review_count} reviews (this is verified — you MAY state it)")
    prices = pricing_rows(bf)
    if prices:
        lines.append("- Pricing (use THESE exact figures wherever a price is quoted; never invent your own):")
        lines += [f"    · {p['item']}: {p['price']}" for p in prices]
    body = "\n".join(lines) or "  (no individual facts set)"
    return ("\n\nVERIFIED BUSINESS FACTS — the ONLY real-world facts you may state:\n"
            f"{body}\n"
            "HARD RULE: any real-world fact NOT listed above (a rating, review/project count, "
            "years in business, phone, license, street address, or a specific price) must be "
            "OMITTED. Never substitute a plausible-looking placeholder. Saying less is required; "
            "fabricating is forbidden.\n")


def allowed_values(bf: BusinessFacts | None) -> set[str]:
    """Verified literal values the trust gate treats as legitimate if they appear
    in generated copy (so we don't flag the owner's own real rating/price)."""
    vals: set[str] = set()
    if not bf:
        return vals
    for v in (bf.phone, bf.license_no, bf.rating, bf.review_count, bf.founded_year):
        if v:
            vals.add(str(v).strip())
    for p in pricing_rows(bf):
        if p["price"]:
            vals.add(p["price"])
    return vals


def facts_block_for(site_id: int) -> str:
    """Convenience for callers without a db session open."""
    db = SessionLocal()
    try:
        return facts_block(get_facts(db, site_id))
    finally:
        db.close()
