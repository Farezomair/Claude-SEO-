# Ascend Doer Registry

Single source of truth for how many doers Ascend has and what each one fixes.
Keep this updated whenever a doer is added/changed.

**Doer count (audit-fixing): 16**
_Last updated: 2026-07-02 — after WebP. Bridge 8 uploaded + verified live. ROADMAP COMPLETE — every planned doer is built._

A "doer" here = a capability that executes or proposes a fix for an audit
finding. Lanes: 🟢 auto (applied + verified live) · 🔵 needs owner approval ·
🟡 owner-only fact (not auto-fixable by design).

## Built (16)

| # | Doer | Module / handler | Fixes (audit categories) | Lane |
|---|------|------------------|--------------------------|------|
| 1 | Meta | `dispatcher._fix_meta` → `wordpress.py` (query-aware via the Keyword Brain) | `meta_title`, `meta_description`, `missing_title`, `meta_description_missing`, `title_length`, `keyword_targeting` | 🟢 |
| 2 | Elementor rewrite | `elementor_agent.run_page_rewrite` | `thin_content`, `eeat_weak`, `content_shallow`, `content_stale`, `geo_unstructured`, `heading_hierarchy`, `missing_h1`, `multiple_h1`, `nap_missing` | 🟢 if safe, else 🔵 |
| 3 | Image dimensions | `image_agent.run_image_dims` | `image_no_dimensions` | 🟢 |
| 4 | Required-pages | `dispatcher._propose_required_page` → `wordpress.create_page` | `required_page_missing` (create + publish) | 🟢 |
| 5 | Linking | `link_agent.run_footer_links` + `context_link_agent.run_context_links` | `required_page_missing` (orphaned → footer link), `low_internal_links` (contextual in-body links, query-aware anchors) | 🟢 |
| 6 | Technical | `technical_agent.run_technical_fixes` (Bridge) | `security_headers`, `no_llms_txt` | 🟢 |
| 7 | Schema | `schema_agent.run_schema_inject` | `no_entity_schema`, `no_localbusiness_schema`, `missing_schema` | 🔵 |
| 8 | Dedupe-title | `dispatcher._propose_dedupe` | `duplicate_title` | 🔵 |
| 9 | Ranking | `dispatcher._propose_ranking` → `brain.improve_meta` | `striking_distance`, `low_ctr` | 🔵 |
| 10 | Alt-text | `alt_agent.run_alt_text` → `brain.generate_alt_texts` | `images_missing_alt` | 🟢 |
| 11 | Redirects | `redirect_agent.run_redirects` → `brain.pick_redirect_targets` + Bridge `/redirects` (v8) | `broken_link` (internal), `broken_page` | 🟢 |
| 12 | Head/meta | `headmeta_agent.run_headmeta` + Bridge `/head` (v8) | `missing_canonical`, `og_incomplete`, `missing_viewport`, `missing_favicon` (favicon best-effort) | 🟢 |
| 13 | Schema-cleanup | `schema_cleanup_agent.run_schema_cleanup` (removes bad JSON-LD from `_meridian_body`) | `schema_invalid`, `schema_placeholder`, `schema_deprecated` | 🟢 |
| 14 | Robots | `robots_agent.run_robots` + Bridge `/robots` full-override (v8) | `ai_crawler_blocked` | 🟢 |
| 15 | Performance | `perf_agent.run_perf` (lazy-load offscreen imgs in `_meridian_body`) | `cwv_poor` | 🟢 applied* |
| 16 | WebP | `webp_agent.run_webp` (imgix `auto=format` or Pillow-convert + WP media rehost) | `image_legacy_format` | 🟢 |

\* Performance applies a real fix (lazy-loading) but does NOT auto-close `cwv_poor` — CWV field data is a ~28-day rolling average, so the finding clears on the next re-measure, not instantly.

**Strategy layer (not a doer): the Keyword Brain** (`keyword_brain.py`, JobRun kind
`keywords`) — builds the target keyword map (business profile + real GSC demand;
`keyword_targets` table). Read by Meta / Ranking / Elementor doers (query-aware
copy) and the `keyword_targeting` audit check. Auto-builds on the weekly run when
empty; manual `POST /sites/{id}/build-keywords`.

Not counted as fix-doers: owner-task router (`_human_task` for `needs_real_data`),
broken-link classifier (`_handle_broken`), on-demand Website CSS doer
(`website_agent.run_change`, not audit-driven).

## Roadmap (to build — see memory `ascend-doer-roadmap`)

Roadmap complete — no planned doers. Remaining non-doer gaps (host-level/server
settings): `no_https`, `mixed_content`, `redirect_issue` (chain-flattening).

Goal: push the audit toward ~100%. The only finding left unfixable by design is
the owner-only `needs_real_data` (real phone, license #, prices — "the fake
number thing").
