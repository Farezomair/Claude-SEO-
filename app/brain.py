"""Claude, the brain. Generates SEO meta titles and descriptions.

Uses the official Anthropic SDK with an API key from ANTHROPIC_API_KEY. The
model defaults to claude-opus-4-8 and can be overridden with ANTHROPIC_MODEL
(e.g. claude-sonnet-4-6 or claude-haiku-4-5) to trade quality for cost.
"""
import json
import os
import re

import anthropic

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

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
