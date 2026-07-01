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
     ]},
    {"key": "schema", "label": "Schema", "weight": 8,
     "desc": "Structured-data presence and validity.",
     "checks": [
         {"label": "Structured data present", "status": "active"},
         {"label": "Valid JSON-LD", "status": "active"},
         {"label": "No placeholder schema", "status": "active"},
         {"label": "No deprecated schema (FAQPage/HowTo)", "status": "active"},
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
     ]},
    {"key": "images", "label": "Images", "weight": 5,
     "desc": "Image health.",
     "checks": [
         {"label": "Width/height set (no layout shift)", "status": "active"},
         {"label": "Modern formats (WebP/AVIF)", "status": "planned"},
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
    {"key": "linking", "agent": "Linking Agent", "lane": "auto", "status": "active",
     "summary": "footer links",
     "desc": "Links orphaned-but-published pages into the footer so the audit, Google, and visitors can reach them. Verified live.",
     "job_kinds": ["linking"]},
    {"key": "redirects", "agent": "Redirects Agent", "lane": "auto", "status": "active",
     "summary": "broken links/pages → 301",
     "desc": "301-redirects dead internal URLs to the most relevant live page (chosen by AI), so broken links and pages resolve. Verified live; external links left for review.",
     "job_kinds": ["redirects"]},
    {"key": "technical", "agent": "Technical Agent", "lane": "auto", "status": "active",
     "summary": "security headers + llms.txt",
     "desc": "Sets security response headers and serves /llms.txt through the Bridge plugin, then re-checks the live site.",
     "job_kinds": ["technical"]},
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
PLANNED_DOERS = [
    {"agent": "Head/meta Agent", "summary": "canonical, Open Graph, favicon",
     "desc": "Adds canonical tags, Open Graph tags, and a favicon."},
    {"agent": "Robots Agent", "summary": "AI crawler access",
     "desc": "Opens robots.txt so AI assistants (GPTBot, etc.) can crawl the site."},
    {"agent": "Schema-cleanup Agent", "summary": "invalid / placeholder / deprecated schema",
     "desc": "Repairs malformed, placeholder, or deprecated structured data."},
    {"agent": "WebP Agent", "summary": "modern image formats",
     "desc": "Converts images to WebP/AVIF for faster loads."},
    {"agent": "Performance Agent", "summary": "Core Web Vitals",
     "desc": "Improves Core Web Vitals, paired with a PageSpeed analyzer."},
]

DOER_COUNT = len(DOERS)
AUDIT_CHECK_COUNT = sum(len(c["checks"]) for c in AUDIT_CATEGORIES)
AUDIT_ACTIVE_COUNT = sum(1 for c in AUDIT_CATEGORIES for ck in c["checks"] if ck["status"] == "active")


def agent_for_job_kind(kind: str) -> str | None:
    """Map a JobRun.kind to its doer's display agent name (for the live pipeline)."""
    for d in DOERS:
        if kind in d["job_kinds"]:
            return d["agent"]
    return None
