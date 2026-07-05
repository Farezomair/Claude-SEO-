"""Single source of truth for Ascend's capabilities — what it AUDITS and the
DOERS that fix it. Powers the read-only "powers" viewer in the header, and the
named-agent pipeline. Mirrors docs/AUDIT-CHECKS.md and docs/DOERS.md; keep all
three in sync when capabilities change.

Read-only for now. The per-capability "enhance" AI bar is deferred until the app
nears 100% (see memory `ascend-doer-roadmap`); this module is the registry it
will hook onto with zero rework.
"""

# ---- Audit: the 8 scored categories, each with the checks under it ----------
# status: "active" = the auditor emits + scores it now; "planned" = defined but
# not yet emitted (lights up when its auditor logic ships).
AUDIT_CATEGORIES = [
    {"key": "technical", "label": "Technical", "weight": 24,
     "desc": "Crawlability and site integrity: broken links/pages, redirects, "
             "HTTPS, canonicals, indexation, mobile viewport, and required pages.",
     "checks": [
         {"label": "Required pages present (privacy/terms/about/contact/accessibility)", "status": "active"},
         {"label": "Security response headers", "status": "active"},
         {"label": "Broken pages (4xx/5xx)", "status": "active"},
         {"label": "Broken links", "status": "active"},
         {"label": "Redirect chains/loops", "status": "active"},
         {"label": "Orphan pages (linked from nowhere)", "status": "active"},
         {"label": "HTTPS enforced", "status": "active"},
         {"label": "Mixed content", "status": "active"},
         {"label": "Canonical tags", "status": "active"},
         {"label": "Indexation (noindex / robots.txt / sitemap)", "status": "active"},
         {"label": "Internal links free of redirect hops", "status": "active"},
         {"label": "Thin archive pages (tag/category/author) not indexable", "status": "active"},
         {"label": "Mobile viewport", "status": "active"},
         {"label": "Header/footer structure", "status": "active"},
     ]},
    {"key": "onpage", "label": "On-page", "weight": 20,
     "desc": "Per-page SEO mechanics and Search Console signals.",
     "checks": [
         {"label": "Title present", "status": "active"},
         {"label": "Meta description present", "status": "active"},
         {"label": "Title length", "status": "active"},
         {"label": "Single H1", "status": "active"},
         {"label": "Heading hierarchy", "status": "active"},
         {"label": "Duplicate titles", "status": "active"},
         {"label": "Stale year in titles", "status": "active"},
         {"label": "Headings parse cleanly (no glued words)", "status": "active"},
         {"label": "Keyword targeting (page matches its target query)", "status": "active"},
         {"label": "Contextual internal links", "status": "active"},
         {"label": "Striking-distance keywords (GSC)", "status": "active"},
         {"label": "Low-CTR pages (GSC)", "status": "active"},
         {"label": "Open Graph tags", "status": "active"},
         {"label": "Favicon", "status": "active"},
         {"label": "Image alt text", "status": "active"},
     ]},
    {"key": "content", "label": "Content & E-E-A-T", "weight": 20,
     "desc": "Depth, freshness, experience/expertise/authority/trust, and the "
             "real-world facts only you can supply.",
     "checks": [
         {"label": "Thin content", "status": "active"},
         {"label": "E-E-A-T signals", "status": "active"},
         {"label": "Content depth for intent", "status": "active"},
         {"label": "Freshness / staleness", "status": "active"},
         {"label": "Owner-only facts (phone, license, prices)", "status": "active"},
         {"label": "No fabricated contact info (fake 555 phone)", "status": "active"},
         {"label": "No placeholder credentials (fake license)", "status": "active"},
         {"label": "No republished duplicate posts (cannibalization)", "status": "active"},
     ]},
    {"key": "schema", "label": "Schema", "weight": 8,
     "desc": "Structured-data presence and validity.",
     "checks": [
         {"label": "Structured data present", "status": "active"},
         {"label": "Valid JSON-LD", "status": "active"},
         {"label": "No placeholder schema", "status": "active"},
         {"label": "No deprecated schema (FAQPage/HowTo)", "status": "active"},
         {"label": "No self-serving review schema (policy risk)", "status": "active"},
         {"label": "No duplicate schema entities", "status": "active"},
     ]},
    {"key": "geo", "label": "AI / GEO", "weight": 8,
     "desc": "Readiness to be found and cited by AI assistants.",
     "checks": [
         {"label": "/llms.txt published", "status": "active"},
         {"label": "Citable content structure", "status": "active"},
         {"label": "Entity (Organization/Website) schema", "status": "active"},
         {"label": "AI crawler access (robots.txt)", "status": "active"},
     ]},
    {"key": "local", "label": "Local", "weight": 5,
     "desc": "Local-business signals.",
     "checks": [
         {"label": "LocalBusiness schema", "status": "active"},
         {"label": "Name/address/phone present", "status": "active"},
         {"label": "Schema address is a real street address", "status": "active"},
         {"label": "Entity corroboration (sameAs / Google Business Profile)", "status": "active"},
     ]},
    {"key": "images", "label": "Images", "weight": 5,
     "desc": "Image health.",
     "checks": [
         {"label": "Width/height set (no layout shift)", "status": "active"},
         {"label": "Modern formats (WebP/AVIF)", "status": "active"},
         {"label": "Real photos, not hotlinked stock presented as projects", "status": "active"},
     ]},
    {"key": "performance", "label": "Performance", "weight": 10,
     "desc": "Core Web Vitals and page-load speed.",
     "checks": [
         {"label": "Core Web Vitals", "status": "active"},
     ]},
]

# ---- Doers: the fix side. `agent` = display name in the pipeline. ----------
# `job_kinds` = JobRun.kind values this doer spawns (for live pipeline tracking);
# empty list = runs inline inside the dispatcher.
DOERS = [
    {"key": "meta", "agent": "Meta Agent", "lane": "auto", "status": "active",
     "summary": "titles & descriptions (Yoast)",
     "desc": "Writes SEO-optimized titles and meta descriptions via Yoast and verifies them live.",
     "job_kinds": []},
    {"key": "elementor", "agent": "Elementor Agent", "lane": "auto", "status": "active",
     "summary": "content / E-E-A-T / headings / GEO",
     "desc": "Rewrites full pages for depth, E-E-A-T, heading structure, and AI-citable answers. "
             "Auto-applies when safety checks pass, otherwise routes to Approvals.",
     "job_kinds": ["elementor"]},
    {"key": "image", "agent": "Image Agent", "lane": "auto", "status": "active",
     "summary": "width / height",
     "desc": "Measures every image and writes width/height into the live page to stop layout shift.",
     "job_kinds": ["image"]},
    {"key": "alttext", "agent": "Alt-text Agent", "lane": "auto", "status": "active",
     "summary": "image alt text",
     "desc": "Writes descriptive, accessibility-first alt text for images missing it, grounded in the filename and nearby text. Auto-applied and verified live.",
     "job_kinds": ["alttext"]},
    {"key": "required_pages", "agent": "Website Agent", "lane": "auto", "status": "active",
     "summary": "create + publish missing pages",
     "desc": "Generates and publishes missing required pages (privacy, terms, about, contact, accessibility) at the right slug.",
     "job_kinds": ["pagedraft"]},
    {"key": "robots", "agent": "Robots Agent", "lane": "auto", "status": "active",
     "summary": "unblock AI crawlers",
     "desc": "Removes robots.txt blocks on AI crawlers (GPTBot, ClaudeBot, PerplexityBot, …) so your pages can be cited by AI search. Verified live.",
     "job_kinds": ["robots"]},
    {"key": "linking", "agent": "Linking Agent", "lane": "auto", "status": "active",
     "summary": "footer + contextual in-body links",
     "desc": "Links orphaned pages into the footer AND adds contextual in-body links between related pages with natural, query-aware anchors. Verified live.",
     "job_kinds": ["linking", "ctxlinks"]},
    {"key": "redirects", "agent": "Redirects Agent", "lane": "auto", "status": "active",
     "summary": "broken links/pages → 301",
     "desc": "301-redirects dead internal URLs to the most relevant live page (chosen by AI), so broken links and pages resolve. Verified live; external links left for review.",
     "job_kinds": ["redirects"]},
    {"key": "technical", "agent": "Technical Agent", "lane": "auto", "status": "active",
     "summary": "security headers + llms.txt",
     "desc": "Sets security response headers and serves /llms.txt through the Bridge plugin, then re-checks the live site.",
     "job_kinds": ["technical"]},
    {"key": "headmeta", "agent": "Head/meta Agent", "lane": "auto", "status": "active",
     "summary": "canonical / Open Graph / viewport / favicon",
     "desc": "Injects the <head> tags a page is missing — self-canonical, Open Graph, mobile viewport, and a favicon — via the Bridge, verified live.",
     "job_kinds": ["headmeta"]},
    {"key": "schemaclean", "agent": "Schema-cleanup Agent", "lane": "auto", "status": "active",
     "summary": "remove broken / placeholder / deprecated schema",
     "desc": "Removes invalid, placeholder, or deprecated (FAQPage/HowTo) JSON-LD from the live page — bad structured data is worse than none. Verified live.",
     "job_kinds": ["schemaclean"]},
    {"key": "perf", "agent": "Performance Agent", "lane": "auto", "status": "active",
     "summary": "lazy-load offscreen images",
     "desc": "Adds native lazy-loading to offscreen images (hero kept eager for LCP) — a safe, no-JS Core Web Vitals win. Applied live; CWV field data reflects it over ~4 weeks.",
     "job_kinds": ["perf"]},
    {"key": "webp", "agent": "WebP Agent", "lane": "auto", "status": "active",
     "summary": "serve images as WebP/AVIF",
     "desc": "Switches JPEG/PNG images to modern formats — image-CDN URLs get format negotiation (auto=format), local files are converted to WebP and rehosted. Verified live.",
     "job_kinds": ["webp"]},
    {"key": "schema", "agent": "Schema Agent", "lane": "approval", "status": "active",
     "summary": "Organization / LocalBusiness JSON-LD",
     "desc": "Generates Organization and LocalBusiness structured data for your review before it goes live.",
     "job_kinds": ["schema"]},
    {"key": "dedupe", "agent": "Dedupe Agent", "lane": "approval", "status": "active",
     "summary": "unique titles",
     "desc": "Proposes a unique title when two pages share one, so they stop competing.",
     "job_kinds": []},
    {"key": "ranking", "agent": "Ranking Agent", "lane": "approval", "status": "active",
     "summary": "GSC click-winners",
     "desc": "Turns Search Console near-miss keywords and low-CTR pages into stronger titles and descriptions.",
     "job_kinds": []},
]

# Roadmap — shown as "planned" cards in the viewer (see memory ascend-doer-roadmap).
# Empty: every roadmap doer is built. Host-level gaps (no_https, mixed_content,
# redirect chains) are server settings, not doers.
PLANNED_DOERS = []

DOER_COUNT = len(DOERS)
AUDIT_CHECK_COUNT = sum(len(c["checks"]) for c in AUDIT_CATEGORIES)
AUDIT_ACTIVE_COUNT = sum(1 for c in AUDIT_CATEGORIES for ck in c["checks"] if ck["status"] == "active")


def agent_for_job_kind(kind: str) -> str | None:
    """Map a JobRun.kind to its doer's display agent name (for the live pipeline)."""
    for d in DOERS:
        if kind in d["job_kinds"]:
            return d["agent"]
    return None


# ---- Tunables: the settings the AI enhance bar may change ------------------
# Declared schema per capability key (doer keys above, or audit category keys
# prefixed "audit:"). The AI can ONLY set these — anything else becomes a
# captured CapabilityRequest. Server-side validation enforces type + range.
TUNABLES = {
    "ranking": [
        {"param": "use_ga4", "type": "bool", "default": False,
         "label": "Use GA4 traffic data",
         "desc": "Enrich ranking proposals with each page's real organic visits from "
                 "Google Analytics 4 (needs Google reconnected with the Analytics scope)."},
        {"param": "gsc_lookback_days", "type": "int", "default": 90, "min": 28, "max": 180,
         "label": "Search Console lookback (days)",
         "desc": "How far back to read Search Console when hunting ranking opportunities."},
    ],
    "elementor": [
        {"param": "max_rewrites_per_run", "type": "int", "default": 20, "min": 1, "max": 40,
         "label": "Max page rewrites per run",
         "desc": "Upper bound on full-page SEO rewrites in one run (each is a large AI call)."},
    ],
    "alttext": [
        {"param": "max_images_per_page", "type": "int", "default": 12, "min": 1, "max": 40,
         "label": "Max images described per page",
         "desc": "How many missing-alt images to describe on one page per run."},
    ],
    "webp": [
        {"param": "quality", "type": "int", "default": 82, "min": 50, "max": 95,
         "label": "WebP quality",
         "desc": "Quality for locally converted WebP images (higher = larger files)."},
    ],
    "audit:content": [
        {"param": "pages_analyzed", "type": "int", "default": 15, "min": 5, "max": 30,
         "label": "Pages deep-read per audit",
         "desc": "How many pages the AI content/E-E-A-T analyzer reads each audit."},
    ],
    "audit:technical": [
        {"param": "crawl_pages", "type": "int", "default": 30, "min": 5, "max": 40,
         "label": "Pages crawled per audit",
         "desc": "How many pages the crawler examines each audit."},
    ],
}


def validate_change(key: str, param: str, value):
    """Coerce+validate one change against the declared tunable. Returns the
    coerced value, or raises ValueError."""
    spec = next((t for t in TUNABLES.get(key, []) if t["param"] == param), None)
    if not spec:
        raise ValueError(f"'{param}' is not a tunable of '{key}'")
    if spec["type"] == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if spec["type"] == "int":
        value = int(value)
        return max(spec["min"], min(spec["max"], value))
    raise ValueError(f"unknown tunable type for '{param}'")


def cap_setting(key: str, param: str, default=None):
    """Read one capability setting (doers call this at run time). Falls back to
    the declared default, then `default`. Never raises."""
    try:
        import json
        from .database import SessionLocal
        from .models import CapabilitySetting
        db = SessionLocal()
        try:
            row = db.query(CapabilitySetting).filter(
                CapabilitySetting.capability_key == key).first()
            if row:
                vals = json.loads(row.settings or "{}")
                if param in vals:
                    return vals[param]
        finally:
            db.close()
    except Exception:
        pass
    spec = next((t for t in TUNABLES.get(key, []) if t["param"] == param), None)
    return spec["default"] if spec and default is None else default
