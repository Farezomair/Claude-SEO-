"""Harvested SEO expertise — the knowledge from the ~/.claude/skills SEO skills,
distilled into prompt-ready guidance the doers inject into their Claude calls.

The deployed app can't invoke the skills at runtime (separate process, no skill
files), so instead we transplant their methodologies/rules/reference data here.
Each block is the actionable core of a skill, kept tight enough to prepend to a
doer's prompt. Sources noted per block; keep current with the skills.
"""

# From seo-page / meta-tags best practices.
META_GUIDE = """META TITLE & DESCRIPTION RULES (expert standard):
- Title: 50-60 characters. Lead with the page's primary keyword/intent. Unique
  across the site. Specific and compelling, not the bare brand name. No clickbait.
- Description: 140-155 characters. Accurately summarize THIS page and invite the
  click with a concrete benefit or call to action. Natural language, no keyword
  stuffing, no quotes around it.
- Match the page's language. Never duplicate another page's title/description."""

# From seo-content (E-E-A-T framework + Who/How/Why + AI-content markers).
EEAT_GUIDE = """CONTENT QUALITY & E-E-A-T (Google helpful-content standard):
Pass the Who/How/Why test: WHO created it (visible author/business identity +
credentials), HOW (first-hand experience, original detail, specifics), WHY (to
genuinely help, not just rank).
- Experience: concrete first-hand detail, specifics, real examples — not generic.
- Expertise: accurate, well-sourced, correct depth for the audience.
- Authoritativeness: clear identity, recognized signals, cite reputable sources.
- Trust: contact info, real specifics, dates, honest claims, no fluff.
Avoid low-quality AI markers: generic phrasing, no specifics, repetitive
structure, no author, padding to a word count. Coverage floors (NOT targets):
homepage ~500w, service page ~800w, blog ~1500w, location page ~500-600w — the
goal is to fully answer intent, not hit a number."""

# From seo-geo (citability + structural readability for AI search).
GEO_GUIDE = """AI-SEARCH / GEO CITABILITY (so ChatGPT, Perplexity & AI Overviews can cite it):
- Put a direct, quotable answer in the FIRST 40-60 words of each section.
- Self-contained passages (~130-170 words) that make sense lifted out of context.
- Include specific facts, numbers, and "X is …"/"X refers to …" definitions.
- Clean H1->H2->H3 hierarchy; question-style headings that match how people ask.
- Short paragraphs (2-4 sentences); use lists and tables for multi-item/comparative data.
- A clear FAQ section answering the real questions for this topic.
GEO is SEO fundamentals applied to AI surfaces — do not keyword-stuff or add
fake "AI" boilerplate; llms.txt and rephrasing tricks do not help."""

# From seo-schema (+ references/deprecated-types). Current as of Feb 2026.
SCHEMA_ACTIVE = [
    "Organization", "LocalBusiness", "Service", "Product", "Offer", "Article",
    "BlogPosting", "Review", "AggregateRating", "BreadcrumbList", "WebSite",
    "WebPage", "Person", "ContactPage", "FAQPage*", "VideoObject", "ImageObject",
    "Event", "JobPosting", "Course",
]
SCHEMA_DEPRECATED = [
    "HowTo", "FAQPage (restricted to gov/health authority sites only)",
    "SpecialAnnouncement", "ClaimReview", "VehicleListing", "Dataset",
    "EstimatedSalary", "CourseInfo", "LearningVideo",
]
SCHEMA_GUIDE = f"""SCHEMA.ORG RULES (JSON-LD only — Google's preference):
- Recommend freely: {", ".join(SCHEMA_ACTIVE)}.
- NEVER use (deprecated / no rich result): {", ".join(SCHEMA_DEPRECATED)}.
- FAQPage rich results are restricted to government/health authority sites; for a
  normal business, keep FAQs as on-page HTML, not FAQPage markup.
- Output valid JSON-LD with @context "https://schema.org", a correct @type, all
  REQUIRED properties for that type, absolute URLs, ISO-8601 dates, and NO
  placeholder text. Put time-sensitive schema (Product/Offer) in server HTML."""

# From seo-images.
IMAGE_GUIDE = """IMAGE SEO RULES:
- Alt text: describe the image content concretely (10-125 chars), include a
  natural keyword where it fits, never stuff. Decorative images get empty alt.
- Set width & height (or aspect-ratio) on every <img> to prevent layout shift (CLS).
- Prefer WebP/AVIF; lazy-load below-the-fold images but NEVER the hero/LCP image.
- Descriptive, hyphenated, lowercase file names (not IMG_1234.jpg)."""
