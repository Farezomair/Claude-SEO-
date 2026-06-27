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
