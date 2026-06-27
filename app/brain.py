"""Claude, the brain. Generates SEO meta titles and descriptions.

Uses the official Anthropic SDK with an API key from ANTHROPIC_API_KEY. The
model defaults to claude-opus-4-8 and can be overridden with ANTHROPIC_MODEL
(e.g. claude-sonnet-4-6 or claude-haiku-4-5) to trade quality for cost.
"""
import json
import os
import re

import anthropic

from .content_standard import BANNED

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# The writing standard, injected into every content-generation prompt.
WRITING_STANDARD = (
    "\n\nWriting standard (MANDATORY):\n"
    "- Never use em dashes (—). Use periods, commas, colons, or parentheses.\n"
    "- Never use these words or close variants: " + ", ".join(BANNED) + ".\n"
    "- No filler openers ('In today's fast-paced world') or closing restatements.\n"
    "- No empty connectives ('it is important to note', 'when it comes to').\n"
    "- Vary sentence length. Be definitive and plain. Every sentence carries information."
)

TITLE_MAX = 60
DESC_MAX = 160

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the model's reply, tolerating fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _rules_block(rules: str) -> str:
    rules = (rules or "").strip()
    return f"\n\nHouse rules you MUST follow:\n{rules}\n" if rules else ""


def generate_meta(page_title: str, page_url: str, content_excerpt: str,
                  site_name: str = "", rules: str = "") -> dict:
    """Return {"title": str, "description": str} for a page."""
    prompt = f"""You are an expert SEO copywriter. Write an SEO meta title and meta description for this web page.{_rules_block(rules)}

Site name: {site_name or "(unknown)"}
Page URL: {page_url}
Page heading / current title: {page_title}

Page content excerpt:
\"\"\"
{content_excerpt}
\"\"\"

Rules:
- Meta title: 50-60 characters. Lead with the page's main topic/keyword. Compelling and specific to THIS page. Do not just repeat the site name.
- Meta description: 140-155 characters. Summarize the page accurately and invite the click. Natural language, no keyword stuffing.
- Write in the same language as the page content.
- No surrounding quotation marks.

Respond with ONLY a JSON object, no preamble and no markdown, in exactly this shape:
{{"title": "...", "description": "..."}}"""

    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system="You are an SEO copywriter. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _extract_json(text)
    title = str(data.get("title", "")).strip().strip('"')[:TITLE_MAX]
    description = str(data.get("description", "")).strip().strip('"')[:DESC_MAX]
    return {"title": title, "description": description}


def generate_article(site_name: str, site_url: str, topic: str = "", rules: str = "") -> dict:
    """Draft a blog post. Returns {"title", "meta_description", "body_html"}."""
    topic_line = f"Write about this topic: {topic}" if topic.strip() else \
        "Choose a useful, search-worthy topic that fits this business and its likely customers."

    prompt = f"""You are an expert SEO content writer creating a blog post for a business website.{_rules_block(rules)}

Business: {site_name}
Website: {site_url}
{topic_line}

Write a complete, genuinely useful blog post (about 600-800 words). Be concrete and helpful to a real reader considering this business's services. Avoid fluff and generic filler.

Format the body as simple HTML using only <h2>, <h3>, <p>, <ul>, <li>, and <strong> tags. Do NOT include an <h1> (the title is separate), and do NOT wrap it in <html> or <body>.
{WRITING_STANDARD}

Respond with ONLY a JSON object, no preamble and no markdown fences, in exactly this shape:
{{"title": "...", "meta_description": "...", "body_html": "..."}}
- title: under 60 characters, compelling.
- meta_description: 140-155 characters."""

    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        system="You are an SEO content writer. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _extract_json(text)
    return {
        "title": str(data.get("title", "")).strip().strip('"')[:TITLE_MAX],
        "meta_description": str(data.get("meta_description", "")).strip().strip('"')[:DESC_MAX],
        "body_html": str(data.get("body_html", "")).strip(),
    }


LEGAL_PAGES = {"privacy", "terms"}
PAGE_BRIEFS = {
    "privacy": "a Privacy Policy",
    "terms": "a Terms of Service (Terms and Conditions) page",
    "about": "an About page introducing the business",
    "contact": "a Contact page",
    "accessibility": "an Accessibility Statement",
}


def generate_page(site_name: str, site_url: str, page_type: str, rules: str = "") -> dict:
    """Draft a missing required page. Returns {"title", "body_html", "legal"}."""
    brief = PAGE_BRIEFS.get(page_type, f"a {page_type} page")
    legal = page_type in LEGAL_PAGES
    if legal:
        guidance = ("This is a legal page. Produce a clear, standard, GENERAL template. "
                    "Use bracketed placeholders like [Business Address], [Contact Email], "
                    "[Jurisdiction] where specifics are needed. Do NOT invent specific legal "
                    "terms, jurisdictions, or data practices — keep it general and leave "
                    "placeholders for the owner to complete with a professional.")
    else:
        guidance = ("Write helpful, specific content for this business. Use bracketed "
                    "placeholders like [phone], [address], [email] for details you don't know.")

    prompt = f"""Write {brief} for the website {site_name} ({site_url}).{_rules_block(rules)}

{guidance}

Format the body as simple HTML using only <h2>, <h3>, <p>, <ul>, <li>, <strong>. Do NOT
include an <h1> (the title is separate) and do NOT wrap it in <html> or <body>.
{WRITING_STANDARD}

Respond with ONLY a JSON object, no preamble, in exactly this shape:
{{"title": "...", "body_html": "..."}}"""

    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=4000,
        system="You write clean website pages. You respond only with a single JSON object.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _extract_json(text)
    return {
        "title": str(data.get("title", "")).strip().strip('"')[:120],
        "body_html": str(data.get("body_html", "")).strip(),
        "legal": legal,
    }


def correct_content(title: str, body_html: str, rules: str = "") -> dict:
    """Editorial cleanup of EXISTING content: strip em dashes, banned vocabulary,
    filler, and padding while PRESERVING meaning and every factual claim.
    Returns {"body_html": cleaned}."""
    prompt = f"""You are a careful copy editor. Rewrite the page content below to meet the
writing standard, WITHOUT changing its meaning, facts, claims, or structure. Do not add
new claims, statistics, or sections. Do not remove information. Only fix the writing.{_rules_block(rules)}
{WRITING_STANDARD}

Page title: {title}

Content (HTML):
\"\"\"
{body_html}
\"\"\"

Keep the same HTML tags and structure. Respond with ONLY a JSON object, no preamble:
{{"body_html": "<the cleaned HTML>"}}"""

    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL, max_tokens=8000,
        system="You are a copy editor. You preserve meaning and respond only with a single JSON object.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _extract_json(text)
    return {"body_html": str(data.get("body_html", "")).strip()}


def generate_css(site_name: str, site_url: str, request: str,
                 current_css: str = "", rules: str = "") -> dict:
    """Produce the COMPLETE new Additional CSS for a visual change request.

    Returns {"css": full_new_css, "summary": short_plain_summary}.
    """
    prompt = f"""You are a careful front-end web developer editing a live WordPress site's "Additional CSS".{_rules_block(rules)}

Site: {site_name} ({site_url})

The owner wants this visual change:
\"\"\"
{request}
\"\"\"

Current Additional CSS (may be empty):
\"\"\"
{current_css}
\"\"\"

Produce the COMPLETE new Additional CSS — the existing CSS plus what's needed for the change. Rules:
- Output valid CSS only (no markdown fences, no HTML).
- Use reasonably specific selectors. Do NOT use !important unless clearly necessary.
- Make the smallest change that achieves the request. Never delete unrelated existing rules.
- This is appended site-wide, so keep it safe and conservative.

Respond with ONLY a JSON object, no preamble, in exactly this shape:
{{"css": "<the full new Additional CSS>", "summary": "<one plain sentence describing what changed>"}}"""

    response = _get_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        system="You are a front-end developer. You respond only with a single JSON object and nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = _extract_json(text)
    return {
        "css": str(data.get("css", "")).strip(),
        "summary": str(data.get("summary", "")).strip()[:300],
    }
