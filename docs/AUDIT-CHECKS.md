# Ascend Audit Check Registry

Single source of truth for how many things Ascend audits and what each check is.
Keep this updated whenever an auditor/check is added or changed. Pairs with
`DOERS.md` (the fix side).

**Audit check count (active): 56** across 5 auditors (incl. the 12-check TRUST LAYER).
_Last updated: 2026-07-01._

Auditors (pipeline in `jobs._run_audit`):
- **Crawler** (`crawler.py`) — 35 checks (homepage + up to 30 pages + site-level)
- **Keyword Brain** (`keyword_brain.py`) — 1 (targeting vs the keyword map)
- **Content / E-E-A-T** (`content_analyzer.py` → Claude, `brain.CONTENT_CATS`) — 5
- **Search Console** (`gsc.py`, when connected) — 2
- **Performance** (`perf.py`, scored only when it runs) — 1

Lane = how the matching doer acts: 🟢 auto · 🔵 approval · 🟡 owner-only · ⚪ no doer yet.

## Trust layer — 12 checks (added after calibrating against an external expert audit)
What real auditors grade hardest: truth, not syntax. CRITICAL trust findings CAP
the category (1 critical → ≤40, 2+ → ≤25) and the OVERALL score
(max(22, 60 − 9·(criticals−1))) — so fabricated data craters the grade the way
Google's quality systems treat it.
| Check | Detects | Sev | Handled by |
|---|---|---|---|
| `fabricated_contact` | fictional 555-01XX phone site-wide | critical | 🟡 Owner |
| `fabricated_credential` | placeholder license/registration number | critical | 🟡 Owner |
| `schema_selfserving_reviews` | LocalBusiness injecting its own aggregateRating (manual-action risk) | critical | 🟢 Schema-cleanup (removes the block) |
| `schema_fake_address` | schema streetAddress isn't a real street address | critical | 🟡 Owner |
| `no_entity_corroboration` | empty sameAs — no GBP/directories/socials | med | 🟡 Owner |
| `schema_duplicate_entity` | 2+ unlinked LocalBusiness entities on a page | med | 🟢 Schema-cleanup |
| `stock_images_hotlinked` | hotlinked stock photos presented as the business's work | high | 🟡 Owner (real photos) |
| `internal_redirect_links` | ≥50% of internal links pass through 301s | med | ⚪ href-rewrite doer (planned) |
| `junk_archives` | tag/category/author archives in the sitemap | med | ⚪ Yoast-settings Bridge ability (planned) |
| `stale_year_title` | title still says 2019–2025 | low | 🟢 Meta |
| `heading_concat` | headings render with glued words (theme markup bug) | med | 🟢 Rewrite |
| `duplicate_post` | /post-2, /post-3 republished duplicates (cannibalization) | med | 🟡 Owner picks survivor → Redirects 301s |

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
| `missing_canonical` | no canonical tag | low | crawler | 🟢 Head/meta |
| `indexation` | noindex / no robots.txt / no sitemap | med/low | crawler | ⚪ review |
| `missing_viewport` | no mobile viewport meta | med | crawler | 🟢 Head/meta |
| `structure` | no header / footer region | med | crawler | ⚪ often false positive |

## On-page — 14 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `missing_title` | no `<title>` | high | crawler | 🟢 Meta |
| `meta_description_missing` | no meta description | med | crawler | 🟢 Meta |
| `title_length` | title too long/short | low | crawler | 🟢 Meta |
| `missing_h1` | no H1 | med | crawler | 🟢 Rewrite |
| `multiple_h1` | more than one H1 | low | crawler | 🟢 Rewrite |
| `heading_hierarchy` | skipped heading levels | low | crawler | 🟢 Rewrite |
| `duplicate_title` | two pages share a title | med | crawler | 🔵 Dedupe-title |
| `keyword_targeting` | page's title/H1 don't reflect its mapped target query | med | keyword-brain | 🟢 Meta (query-aware) |
| `low_internal_links` | almost no contextual in-body links | low | crawler | 🟢 Linking (contextual) |
| `striking_distance` | GSC: page ranking just off page 1 | — | gsc | 🔵 Ranking |
| `low_ctr` | GSC: impressions but low CTR | — | gsc | 🔵 Ranking |
| `og_incomplete` | missing Open Graph tags | low | crawler | 🟢 Head/meta |
| `missing_favicon` | no favicon | low | crawler | 🟢 Head/meta (best-effort — reuses a logo image) |
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
| `schema_invalid` | malformed JSON-LD | med | crawler | 🟢 Schema-cleanup |
| `schema_placeholder` | placeholder text in schema | med | crawler | 🟢 Schema-cleanup |
| `schema_deprecated` | deprecated FAQPage/HowTo | low | crawler | 🟢 Schema-cleanup |

## AI / GEO — 4 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `no_llms_txt` | no `/llms.txt` | low | crawler | 🟢 Technical |
| `geo_unstructured` | not citable by AI answers | low–high | content | 🟢 Rewrite |
| `no_entity_schema` | no Organization/Website entity | med | crawler | 🔵 Schema |
| `ai_crawler_blocked` | robots.txt blocks GPTBot/etc. | med | crawler | 🟢 Robots |

## Local — 2 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `no_localbusiness_schema` | no LocalBusiness markup | low | crawler | 🔵 Schema |
| `nap_missing` | name/address/phone absent | low | crawler | 🟢 Rewrite (real NAP → 🟡) |

## Images — 2 checks
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `image_no_dimensions` | no width/height (layout shift) | low | crawler | 🟢 Image dimensions |
| `image_legacy_format` | JPEG/PNG where WebP/AVIF could serve (imgix CDN without auto=format, or plain .jpg/.png) | low | crawler | 🟢 WebP |

## Performance — 1 check
| Check (category) | Detects | Sev | Source | Handled by |
|---|---|---|---|---|
| `cwv_poor` | poor Core Web Vitals | — | perf | 🟢 Performance (lazy-load; CWV field data lags ~4wk) |

## Defined but not yet emitted (2)
Categories the router/scorer already understands but no active auditor produces
yet — they light up when the matching auditor logic is added:
`meta_title`, `meta_description` (sitewide meta scan).

---
Note: some categories bundle multiple sub-checks (`indexation` = noindex +
robots.txt + sitemap; `structure` = header + footer; `broken_link` = internal +
external at several severities), so the literal number of probes run per page is
higher than 56 — but 56 is the count of distinct routable/scored check-types.
