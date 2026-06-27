"""Finding routing + classification (Phase A).

Turns a raw audit category into the structured fields the architecture runs on:
which doer owns the fix (route), how risky it is to apply (action_class), and
which check group it belongs to. This encodes the routing table from the
Website Auditor / SEO Auditor specs in docs/agent-scopes.md.

As later phases add real auditors and doers, this table grows; for Phase A it
covers the categories the current crawler and meta scan produce.
"""

# category -> (group, route, action_class)
_TABLE = {
    # Site integrity (group A) -> Website Agent
    "broken_page": ("site-integrity", "Website Agent", "auto-safe"),
    "broken_link": ("site-integrity", "Website Agent", "auto-safe"),
    # Redirects -> SEO Technical (single owner of redirect/canonical execution)
    "redirect_issue": ("site-integrity", "SEO Technical", "needs-approval"),
    # On-page mechanics / structure (group G) -> Website Agent
    "structure": ("structure", "Website Agent", "needs-approval"),
    "missing_h1": ("on-page", "Website Agent", "needs-approval"),
    "multiple_h1": ("on-page", "Website Agent", "needs-approval"),
    "missing_viewport": ("mobile", "Website Agent", "needs-approval"),
    "missing_favicon": ("on-page", "Website Agent", "auto-safe"),
    "images_missing_alt": ("on-page", "Website Agent", "auto-safe"),
    # Crawl & index (group B) -> SEO Technical; index directives forced to gate
    "indexation": ("indexation", "SEO Technical", "needs-approval"),
    "missing_canonical": ("indexation", "SEO Technical", "auto-safe"),
    # Meta hygiene (sitewide missing/duplicate) -> SEO Technical
    "meta_title": ("meta", "SEO Technical", "auto-safe"),
    "meta_description": ("meta", "SEO Technical", "auto-safe"),
    "missing_title": ("meta", "SEO Technical", "needs-approval"),
    "duplicate_title": ("meta", "SEO Technical", "needs-approval"),
    # Required pages (group C) -> Website Agent (legal copy gated)
    "required_page_missing": ("required-pages", "Website Agent", "needs-approval"),
    # Security (group F) -> Website Agent
    "no_https": ("security", "Website Agent", "needs-approval"),
    "mixed_content": ("security", "Website Agent", "needs-approval"),
    "security_headers": ("security", "Website Agent", "needs-approval"),
    # Orphan pages (group A) -> Website Agent
    "orphan_page": ("site-integrity", "Website Agent", "needs-approval"),
    # Open Graph / social tags (group D) -> SEO On-page
    "og_incomplete": ("meta", "SEO On-page", "needs-approval"),
    # Ranking signals from Search Console (SEO Auditor groups H/I) -> SEO On-page
    "striking_distance": ("ranking", "SEO On-page", "needs-approval"),
    "low_ctr": ("ranking", "SEO On-page", "needs-approval"),
    # Content depth (SEO Auditor group C) -> Content Corrector
    "thin_content": ("content-depth", "Content Corrector", "needs-approval"),
    # Schema validity/richness (group F) -> SEO Technical
    "missing_schema": ("schema", "SEO Technical", "needs-approval"),
    "schema_invalid": ("schema", "SEO Technical", "needs-approval"),
    "schema_placeholder": ("schema", "SEO Technical", "auto-safe"),
    "schema_deprecated": ("schema", "SEO Technical", "auto-safe"),
    # On-page depth (expanded battery)
    "meta_description_missing": ("meta", "SEO Technical", "auto-safe"),
    "title_length": ("meta", "SEO Technical", "auto-safe"),
    "heading_hierarchy": ("on-page", "SEO On-page", "needs-approval"),
    "low_internal_links": ("on-page", "SEO On-page", "needs-approval"),
    # Images (group D)
    "image_no_dimensions": ("images", "Website Agent", "needs-approval"),
    "image_legacy_format": ("images", "Website Agent", "needs-approval"),
    # AI / GEO readiness
    "ai_crawler_blocked": ("ai-geo", "SEO Technical", "needs-approval"),
    "no_llms_txt": ("ai-geo", "SEO On-page", "needs-approval"),
    "no_entity_schema": ("ai-geo", "SEO Technical", "needs-approval"),
    "geo_unstructured": ("ai-geo", "SEO On-page", "needs-approval"),
    # Local SEO
    "no_localbusiness_schema": ("local", "SEO Technical", "needs-approval"),
    "nap_missing": ("local", "Website Agent", "needs-approval"),
    # Content / E-E-A-T (Claude analyzer, phase 2)
    "eeat_weak": ("content", "Content Corrector", "needs-approval"),
    "content_shallow": ("content", "Content Writer", "needs-approval"),
    "content_stale": ("content", "Content Corrector", "needs-approval"),
    # Performance (PageSpeed analyzer, phase 2)
    "cwv_poor": ("performance", "Website Agent", "needs-human"),
}

_DEFAULT = ("uncategorized", "Website Agent", "needs-approval")


def classify(category: str) -> dict:
    group, route, action_class = _TABLE.get(category, _DEFAULT)
    return {"group": group, "route": route, "action_class": action_class}
