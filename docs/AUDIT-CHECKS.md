# Ascend Audit Check Registry

Single source of truth for how many things Ascend audits and what each check is.
Keep this updated whenever an auditor/check is added or changed. Pairs with
`DOERS.md` (the fix side).

**Audit check count (active): 41** across 4 auditors.
_Last updated: 2026-07-01._

Auditors (pipeline in `jobs._run_audit`):
- **Crawler** (`crawler.py`) — 33 checks (homepage + up to 15 pages + site-level)
- **Content / E-E-A-T** (`content_analyzer.py` → Claude, `brain.CONTENT_CATS`) — 5
- **Search Console** (`gsc.py`, when connected) — 2
- **Performance** (`perf.py`, scored only when it runs) — 1

Lane = how the matching doer acts: 🟢 auto · 🔵 approval · 🟡 owner-only · ⚪ no doer yet.

## Technical — 12 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `required_page_missing` | privacy/terms/about/contact/accessibility unreachable | med/low | crawler | 🟢 Required-pages + Internal-linking |
| `security_headers` | HSTS/CSP/X-Frame/etc. missing | low | crawler | 🟢 Technical |
| `broken_page` | page returns 4xx/5xx | high | crawler | 🟢 Redirects (internal) |
| `broken_link` | dead internal/external link | high/low | crawler | 🟢 Redirects (internal; external → review) |
| `redirect_issue` | redirect chain/loop | low | crawler | ⚪ (chain-flattening not built) |
| `orphan_page` | page reachable from no internal link | low | crawler | ⚪ (extend Internal-linking) |
| `no_https` | HTTPS not enforced | high | crawler | ⚪ host-level |
| `mixed_content` | http assets on https page | med | crawler | ⚪ host-level |
| `missing_canonical` | no canonical tag | low | crawler | ⚪ (head/meta doer) |
| `indexation` | noindex / no robots.txt / no sitemap | med/low | crawler | ⚪ review |
| `missing_viewport` | no mobile viewport meta | med | crawler | ⚪ theme-level |
| `structure` | no header / footer region | med | crawler | ⚪ often false positive |

## On-page — 12 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `missing_title` | no `<title>` | high | crawler | 🟢 Meta |
| `meta_description_missing` | no meta description | med | crawler | 🟢 Meta |
| `title_length` | title too long/short | low | crawler | 🟢 Meta |
| `missing_h1` | no H1 | med | crawler | 🟢 Rewrite |
| `multiple_h1` | more than one H1 | low | crawler | 🟢 Rewrite |
| `heading_hierarchy` | skipped heading levels | low | crawler | 🟢 Rewrite |
| `duplicate_title` | two pages share a title | med | crawler | 🔵 Dedupe-title |
| `striking_distance` | GSC: page ranking just off page 1 | — | gsc | 🔵 Ranking |
| `low_ctr` | GSC: impressions but low CTR | — | gsc | 🔵 Ranking |
| `og_incomplete` | missing Open Graph tags | low | crawler | ⚪ (head/meta doer) |
| `missing_favicon` | no favicon | low | crawler | ⚪ (head/meta doer) |
| `images_missing_alt` | images without alt text | low | crawler | 🟢 Alt-text |

## Content & E-E-A-T — 5 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `thin_content` | page below content threshold | med | crawler | 🟢 Rewrite |
| `eeat_weak` | weak expertise/trust signals | low–high | content | 🟢 Rewrite |
| `content_shallow` | lacks depth for intent | low–high | content | 🟢 Rewrite |
| `content_stale` | dated / needs freshening | low–high | content | 🟢 Rewrite |
| `needs_real_data` | needs real phone/license/price/etc. | low–high | content | 🟡 Owner-only |

## Schema — 4 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `missing_schema` | no structured data on page | low | crawler | 🔵 Schema |
| `schema_invalid` | malformed JSON-LD | med | crawler | ⚪ (schema-cleanup doer) |
| `schema_placeholder` | placeholder text in schema | med | crawler | ⚪ (schema-cleanup doer) |
| `schema_deprecated` | deprecated FAQPage/HowTo | low | crawler | ⚪ (schema-cleanup doer) |

## AI / GEO — 4 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `no_llms_txt` | no `/llms.txt` | low | crawler | 🟢 Technical |
| `geo_unstructured` | not citable by AI answers | low–high | content | 🟢 Rewrite |
| `no_entity_schema` | no Organization/Website entity | med | crawler | 🔵 Schema |
| `ai_crawler_blocked` | robots.txt blocks GPTBot/etc. | med | crawler | ⚪ (robots doer) |

## Local — 2 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `no_localbusiness_schema` | no LocalBusiness markup | low | crawler | 🔵 Schema |
| `nap_missing` | name/address/phone absent | low | crawler | 🟢 Rewrite (real NAP → 🟡) |

## Images — 1 check
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `image_no_dimensions` | no width/height (layout shift) | low | crawler | 🟢 Image dimensions |

## Performance — 1 check
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `cwv_poor` | poor Core Web Vitals | — | perf | ⚪ (performance doer) |

## Defined but not yet emitted (4)
Categories the router/scorer already understands but no active auditor produces
yet — they light up when the matching auditor logic is added:
`meta_title`, `meta_description` (sitewide meta scan), `low_internal_links`
(on-page link-depth), `image_legacy_format` (WebP/AVIF detection).

---
Note: some categories bundle multiple sub-checks (`indexation` = noindex +
robots.txt + sitemap; `structure` = header + footer; `broken_link` = internal +
external at several severities), so the literal number of probes run per page is
higher than 41 — but 41 is the count of distinct routable/scored check-types.
