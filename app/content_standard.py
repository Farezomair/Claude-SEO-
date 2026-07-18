"""The writing standard, enforced mechanically (Phase D).

Every piece of content the system produces or corrects must read zero banned
terms and zero em dashes before it can be staged. This module is the gate: it
scans text, strips em dashes, and reports what's left, so the content agents can
assert the standard on output rather than hope for it.
"""
import re

EM_DASH = "—"  # —
EN_DASH = "–"  # – (only flagged when used as a sentence dash, not in ranges)

# Default banned AI/marketing vocabulary (per-site extendable later via rules).
BANNED = [
    "leverage", "harness", "elevate", "empower", "unlock", "seamless",
    "cutting-edge", "game-changing", "game changer", "next-level", "world-class",
    "holistic", "synergy", "revolutionize", "revolutionise", "innovative",
    "transformative", "tapestry", "testament to", "realm of", "delve",
    "navigating the", "in today's", "ever-evolving", "fast-paced", "robust",
    "unleash", "supercharge", "turbocharge", "best-in-class", "bespoke",
]
_BANNED_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BANNED) + r")", re.I)


# ── Fabricated trust/quantitative claims (the hard fact-accuracy constraint) ──
# Numbers paired with trust words are the fingerprint of invented "typical local
# business" boilerplate: "80+ reviews", "5.0 rating", "40+ projects", "12+ years".
# Detected in generated copy and rejected unless the exact value is a verified fact.
_TRUST_CLAIM_RES = [
    re.compile(r"\b\d(?:\.\d)?\s*(?:/\s*5)?\s*(?:star|stars|★)\b", re.I),
    re.compile(r"\b\d(?:\.\d)?\s*(?:google|facebook|yelp)?\s*rating\b", re.I),
    re.compile(r"\b\d{1,6}\s*\+?\s*(?:reviews?|ratings?|testimonials?)\b", re.I),
    re.compile(r"\b\d{1,6}\s*\+?\s*(?:projects?|jobs?|installs?|installations?|clients?|customers?|homeowners?)\b", re.I),
    re.compile(r"\b\d{1,3}\s*\+?\s*years?\s+(?:of\s+)?(?:experience|in\s+business|serving)\b", re.I),
    re.compile(r"\bsince\s+(?:19|20)\d{2}\b", re.I),
    re.compile(r"\bA\+\s*(?:BBB|rating)\b", re.I),
    re.compile(r"\b(?:award[- ]winning|#\s*1\s+rated|top[- ]rated)\b", re.I),
]
# Placeholder real-world identifiers presented as real.
_PLACEHOLDER_FACT_RES = [
    re.compile(r"\(?\d{3}\)?[\s.\-]?555[\s.\-]?01\d{2}"),                       # fictional 555 phone
    re.compile(r"(?:lic(?:ense|ence)?\.?|reg(?:istration)?\.?)\s*#?\s*[A-Z]{0,4}-?(?:12345|00000|1234)\b", re.I),
]


def scan_trust(text: str, allowed: set | None = None) -> list[str]:
    """Return fabricated quantitative-trust or placeholder-fact claims found in
    `text`, excluding any whose literal value is in `allowed` (verified facts)."""
    allowed = allowed or set()
    hits: list[str] = []
    for rx in _TRUST_CLAIM_RES + _PLACEHOLDER_FACT_RES:
        for m in rx.finditer(text or ""):
            frag = m.group(0).strip()
            if not any(a and a in frag for a in allowed):
                hits.append(frag)
    # de-dupe, preserve order
    seen, out = set(), []
    for h in hits:
        k = h.lower()
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def scan(text: str) -> dict:
    """Return {"banned": [...distinct terms...], "em_dashes": int}."""
    found = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text or "")})
    return {"banned": found, "em_dashes": (text or "").count(EM_DASH)}


def passed(text: str) -> bool:
    s = scan(text)
    return not s["banned"] and s["em_dashes"] == 0


def strip_em_dashes(text: str) -> str:
    """Mechanical fallback: replace an em dash (with its spaces) by a comma."""
    if not text:
        return text
    text = re.sub(r"\s*" + EM_DASH + r"\s*", ", ", text)
    # Tidy any doubled punctuation the replacement may create.
    return re.sub(r",\s*([.,;:])", r"\1", text)
