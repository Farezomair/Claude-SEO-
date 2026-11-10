"""Write one Facebook post from a source page with Claude, then check it against the rules."""
import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..outreach import llm

KIND_BRIEF = {
    "new_guide": "Announce this new guide: the question it answers and who it is for, with one concrete fact or step "
                 "from it. Close with a short plain line pointing to the guide.",
    "diagram": "Build the post around the picture you choose: say in plain words what it shows and why it matters "
               "to a treatment center owner or admissions director.",
    "stat": "Lead with one number from the source, name where it comes from the way the source names it, then say "
            "what it means for a treatment center.",
    "checklist": "A short numbered checklist (3 to 8 items, one line each) taken from the source, with one line "
                 "before it saying who it is for.",
    "page": "Explain what this service does for a treatment center and who it is for, using only what the page says. "
            "No prices or results unless the page states them, and no promises.",
}

RULES = """You write Facebook posts for {brand}'s business page. Audience: {audience}.

Rules, all of them hard:
- 40 to 110 words. Plain words an owner or admissions director uses; explain any SEO term you use.
- Every number, percentage, dollar figure, law or source you mention must appear in the source text below,
  exactly as it is there. Never round, estimate or invent. If the source has no number that fits, use none.
- No em dashes or en dashes; use commas, colons or periods. No exclamation marks.
- Never patient stories, testimonials about treatment outcomes, or fear-based lines aimed at people seeking help.
- Never promise or guarantee rankings, AI answers, calls or admissions. No "number one" or "#1".
- Do not mention {exclude}.
- No links or web addresses in the text: the link is added after your text automatically.
- Hashtags: none, or one at the very end.
- Do not repeat the opening or angle of the recent posts listed.
- Match the voice of the example posts: direct, specific, calm, useful on its own even without clicking."""


class FbDraft(BaseModel):
    message: str = Field(description="the post text, 40 to 110 words, no link")
    image_index: int = Field(description="index of the picture to attach from the list, or -1 for none")
    support: list[str] = Field(description="for each number or fact in the message, the exact sentence from the source text that states it")


def utm_link(url: str, source: str = "facebook") -> str:
    slug = [s for s in urlparse(url).path.split("/") if s][-1:] or ["home"]
    return f"{url.split('?')[0]}?utm_source={source}&utm_medium=social&utm_campaign={slug[0]}"


_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def _norm_num(s):
    return s.replace(",", "").replace("$", "").rstrip("%").rstrip(".")


def check(message: str, src_text: str, cfg: dict, needs_image: bool, image_index: int, n_images: int) -> list[str]:
    p = []
    words = len(re.findall(r"\b\w[\w'%$.,]*\b", message))
    if not 30 <= words <= 130:
        p.append(f"length {words} words (want 40 to 110)")
    if re.search("[–—]", message):
        p.append("contains an em or en dash")
    if "!" in message:
        p.append("contains an exclamation mark")
    if re.search(r"https?://|www\.|\.com\b", message, re.I):
        p.append("contains a link or web address")
    if len(re.findall(r"(?<!\w)#\w+", message)) > 1:
        p.append("more than one hashtag")
    low = message.lower()
    for w in cfg.get("banned_words", []) + cfg.get("exclude_words", []):
        if re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", low):
            p.append(f"uses '{w}'")
    src_nums = {_norm_num(n) for n in _NUM.findall(src_text)}
    for line in message.splitlines():
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)  # list numbering is not a claim
        for n in _NUM.findall(line):
            if _norm_num(n) not in src_nums:
                p.append(f"number {n} is not in the source")
    if needs_image and not (0 <= image_index < n_images):
        p.append("no picture chosen")
    return p


def draft(kind: str, src: dict, cfg: dict, recent: list[str], used_images: set, max_rounds: int = 3) -> dict:
    """Returns {message, image_url, link, problems}. problems empty = passed every check."""
    images = [im for im in src.get("images", []) if im["url"] not in used_images] or src.get("images", [])
    needs_image = kind in ("diagram", "new_guide", "stat", "checklist") and bool(images)
    pics = "\n".join(f"[{i}] {im['alt'] or im['url'].rsplit('/', 1)[-1]}" for i, im in enumerate(images)) or "(none)"
    system = RULES.format(brand=cfg["brand"] or "the company", audience=cfg["audience"] or "business owners",
                          exclude=", ".join(cfg.get("exclude_words") or []) or "nothing in particular")
    base = (f"Post type: {kind}. {KIND_BRIEF[kind]}\n\n"
            f"Example posts (voice only, never reuse their facts):\n{cfg.get('style_examples') or '(none)'}\n\n"
            f"Recent posts (do not repeat):\n" + ("\n---\n".join(recent[-6:]) or "(none)") + "\n\n"
            f"Pictures you can attach ({'choose one' if needs_image else 'optional'}):\n{pics}\n\n"
            f"Source: {src['title']}\n{src['url']}\n\nSource text:\n{src['text'][:14000]}")
    feedback, last = "", None
    for _ in range(max_rounds):
        d = llm.structured(system, base + feedback, FbDraft, effort="medium", max_tokens=4000)
        msg = d.message.strip()
        probs = check(msg, src["text"], cfg, needs_image, d.image_index, len(images))
        img = images[d.image_index]["url"] if 0 <= d.image_index < len(images) else ""
        last = {"message": msg, "image_url": img, "link": utm_link(src["url"], cfg.get("utm_source", "facebook")),
                "problems": probs}
        if not probs:
            return last
        feedback = "\n\nYour last draft broke these rules, write it again and fix them:\n- " + "\n- ".join(probs) + \
                   f"\n\nLast draft:\n{msg}"
    return last
