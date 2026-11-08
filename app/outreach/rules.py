"""Automatic checks every outgoing cold email must pass. Failing emails go to needs_review."""
import re

BANNED = [
    "guarantee", "guaranteed", "#1", "number one", "skyrocket", "explode", "revolutionary", "game-changer",
    "game changer", "limited time", "act now", "urgent", "don't miss", "patient", "patients", "outcome",
    "outcomes", "cure", "recovery rate", "success rate", "case study", "case studies", "our clients",
    "dominate", "crush", "10x", "best in", "world-class", "cutting-edge", "synergy", "leverage",
]
DASHES = ["—", "–", " - ", "--"]


def _cfg():
    from . import config
    return config.load()


def sign_offs():
    s = _cfg()["sender"]
    return ("All the best,", s.get("first_name") or "", s.get("name") or "")
WORD_LIMITS = {"email1": 150}  # everything else: 120

# Words an owner or admissions director shouldn't have to decode in the opening.
JARGON = ["seo", "h1", "meta", "keyword", "serp", "backlink", "rank", "ranking", "rankings", "indexed",
          "crawl", "schema", "template text", "landing page", "conversion", "cta", "ux", "url"]


def body_words(body: str) -> int:
    """Words above the signature."""
    main = []
    for line in body.splitlines():
        if line.strip() and line.strip() in sign_offs():
            break
        main.append(line)
    return len(re.findall(r"[A-Za-z0-9'][A-Za-z0-9'.&-]*", "\n".join(main)))


def jargon_in(text: str) -> list[str]:
    low = text.lower()
    return [w for w in JARGON if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low)]


def check(kind: str, subject: str, body: str, slots: dict) -> list[str]:
    problems = []
    text = f"{subject}\n{body}"
    low = text.lower()
    for d in DASHES:
        if d in text:
            problems.append(f"contains a dash ({d!r})")
    for w in BANNED:
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low):
            problems.append(f"banned word: {w}")
    links = re.findall(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|org|net|io|co)\b\S*)", low)
    cfg = _cfg()
    site = (cfg["company"].get("website") or "").lower().removeprefix("https://").removeprefix("www.").strip("/")
    cal = (cfg["company"].get("calendly") or "").lower().removeprefix("https://").strip("/")
    allowed = {site} if site else set()
    for l in links:
        clean = re.sub(r"^https?://", "", l).rstrip(".,)")
        if clean in allowed or (cal and kind in ("audit", "pa_fu1") and clean.startswith(cal)):
            continue
        problems.append(f"link not allowed: {l}")
    if kind in ("email1", "fu1", "fu2"):
        addr = (cfg["company"].get("mailing_address") or "").split(",")[0]
        if not addr or addr not in body:
            problems.append("mailing address missing")
        if 'reply "no"' not in body.lower():
            problems.append("opt-out line missing")
    n = body_words(body)
    if n > WORD_LIMITS.get(kind, 120):
        problems.append(f"{n} words (limit {WORD_LIMITS.get(kind, 120)})")
    if "{" in text or "}" in text:
        problems.append("unfilled template slot")
    name = slots.get("name", "")
    if name and name != "there" and not re.fullmatch(r"[A-Z][a-zA-Z'\-]{1,20}", name):
        problems.append(f"odd first name: {name!r}")
    return problems
