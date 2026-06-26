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
    # Site integrity (Website Auditor group A) -> Website Agent
    "broken_page": ("site-integrity", "Website Agent", "auto-safe"),
    "broken_link": ("site-integrity", "Website Agent", "auto-safe"),
    # On-page mechanics / structure (group G) -> Website Agent
    "structure": ("structure", "Website Agent", "needs-approval"),
    # Crawl & index (group B) -> SEO Technical; index directives forced to gate
    "indexation": ("indexation", "SEO Technical", "needs-approval"),
    # Meta hygiene (sitewide missing/duplicate) -> SEO Technical, mechanical
    "meta_title": ("meta", "SEO Technical", "auto-safe"),
    "meta_description": ("meta", "SEO Technical", "auto-safe"),
}

_DEFAULT = ("uncategorized", "Website Agent", "needs-approval")


def classify(category: str) -> dict:
    group, route, action_class = _TABLE.get(category, _DEFAULT)
    return {"group": group, "route": route, "action_class": action_class}
