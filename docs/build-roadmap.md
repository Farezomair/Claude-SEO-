# Ascend — Phased Build Roadmap

How we get from today's app to the 11-agent architecture in [agent-scopes.md](agent-scopes.md).
Same discipline as stages 0–6: **one phase at a time, each ships and is proven on a
real site (Meridian) before the next begins.** We reuse the current app foundation and
the sibling `claude-seo` skills/scripts wherever they fit instead of rebuilding.

## Principles
- **Ship + prove each phase.** No phase is "done" until it runs on Meridian in the browser.
- **Data model first.** Phase A builds the Finding/Fix rails; every later agent plugs in.
- **Free data before paid.** Stand up everything that runs on a direct crawl + free Google
  APIs first. Paid sources (backlinks, SERP/keyword, DataForSEO) come in their own phase;
  until then the dependent audit groups run in **limited** mode and say so.
- **Reversible + gated by default.** Snapshot before every write; the approval gate already
  exists and extends to every doer.
- **Lean on what exists.** Today's `crawler`, `seo_technical` meta fixer, `wordpress`
  adapter, `approvals`, `scheduler`/`weekly`, and the helper plugin are real, working slices.

## Today → architecture mapping
| Exists now | Is the embryo of |
|---|---|
| `crawler.py` audit | Website Auditor (a few of its 9 groups) |
| `seo_technical.py` meta fixer | SEO Technical (one slice: meta) |
| `wordpress.py` + helper plugin | the CMS API adapter |
| `website_agent.py` CSS engine | Website Agent (gated mobile/stylesheet path) |
| `content_agent.py` + approvals | Content Writer + the approval gate |
| `scheduler.py` + `weekly.py` + `JobRun` | the Conductor (weekly trigger + a 3-phase loop) |
| `weekly._build_report` | Report Generator (deterministic summary) |

---

## Phase A — Foundation: the Finding/Fix data model ⭐ keystone
**Goal:** introduce the structured spine so every agent speaks the same language.

- **New models:** `Finding` (id `WA-/SA-/LA-…`, group, severity, halt, finding_type,
  route, cross_route, action_class, evidence JSON, status, detection_source);
  `FixRecord` (fix_id, issue_id, doer, action_taken, before/after snapshot refs, method,
  lane, approved_by, verify_hint, outcome_pending, status); extend `JobRun` into a proper
  `Run` with the 7 phases + the run-record fields.
- **Refactor existing flow onto the rails:** the crawler emits `Finding`s; the meta fixer
  emits `FixRecord`s; add **verification mode** (re-check acted-on IDs → verified/not-fixed/
  partial/regressed); reshape `weekly.py` into the Conductor's phase skeleton (audit →
  intake/route → gate-sort → dispatch → compile → verify → re-work/close).
- **Snapshot + rollback** store, keyed by issue ID, used by every doer.
- **Deliverable / proof:** Meridian runs the current capabilities (crawl audit + meta fix +
  report) but now produces real Findings, routed, with a working verify loop and rollback.

## Phase B — The two Auditors (free data)
**Goal:** the real detection layer.

- **Website Auditor:** build out groups A–I (integrity, crawl/index, required pages,
  speed, mobile, security, on-page mechanics, schema presence, EEAT presence). Uses
  `seo-technical`, `seo-content`(EEAT), `seo-schema`, `seo-sitemap`, `seo-images`,
  scripts `pagespeed_check`, `gsc_query`/`gsc_inspect`, `capture_screenshot`/visual.
- **SEO Auditor:** stand up the free-data groups (A keyword/intent via GSC, B on-page
  quality, D meta/snippet, E cannibalization via GSC, F schema validity, G AEO, I ranking).
  Groups J/K and competitive parts of C/G marked **limited** until Phase E. Uses `seo-page`,
  `seo-content`, `seo-cluster`, `seo-geo`, `seo-google`, `seo-sxo`.
- **Connectors (free):** Google Search Console (OAuth per site), PageSpeed Insights / CrUX
  (API key). GSC is the workhorse and is free.
- **Deliverable / proof:** both auditors produce a routed, severity-tagged finding list for
  Meridian, visible in the app, with the limited groups clearly labeled.

## Phase C — The SEO doers + the full Website Agent
**Goal:** close the audit → fix → verify loop for real.

- **SEO Technical:** the 6-group fix battery (crawl/index directives, redirects, schema
  generate via `schema_generate`/`seo-schema`, CWV config, meta hygiene at scale, verify
  prep) through the adapter layer; the index/redirect safety floor; batch-with-hold.
- **SEO On-page:** targeting + title/meta rewriting + headings + the **four-anchor
  discipline** self-check + AEO/GEO formatting; cannibalization decision ownership.
- **Website Agent:** expand to the full **3-lane autonomy model** (autonomous/gated/
  hard-stop) + the 6-group battery (integrity, required pages, header/footer, security,
  mobile, trust copy gated); per-site autonomy dial.
- **Adapters:** formalize CMS-API (have it), add file + host/CDN + **ticket** adapters.
- **Deliverable / proof:** Meridian gets autonomous Website-Agent fixes + gated SEO fixes,
  all verified by the auditors and rolled back on regression.

## Phase D — The content team
**Goal:** prose agents with the writing standard enforced.

- **Content Writer** (depth, net-new, rewrites, merges, GEO content) + **Content Corrector**
  (style-contract enforcement, factual freshening, de-stuffing, bulk sweep with sample-hold).
- **Mechanical writing standard:** banned-words + em-dash check must read zero before stage
  (`content_humanize`, `content_quality`); **CORE-EEAT gate** returns SHIP/FIX/BLOCK; YMYL
  subject-matter review gate (dormant for Meridian).
- **Deliverable / proof:** a thin Meridian page expanded + a legacy page corrected, both
  passing the style + quality gates, gated before publish.

## Phase E — Local + Off-page (connector-heavy, partly paid)
**Goal:** the off-site coverage.

- **Local Agent:** the GBP/citation/review/local-page battery + **its own detection pass**;
  holds the canonical identity record; suspension-risk fields hard-stopped. **Build the GBP
  API connector** (the one real capability with no skill). Uses `seo-local`, `seo-maps`.
- **SEO Off-page:** backlinks/toxic/disavow (never auto-submit) + link acquisition drafts +
  entity building. **Backlink source:** Moz + Bing free tiers first (`moz_api`,
  `bing_webmaster`, `commoncrawl_graph`), DataForSEO paid optional.
- **Deliverable / proof:** Meridian's GBP audited + NAP synced to canonical, a disavow file
  drafted (not submitted), all gated/human as specified.

## Phase F — Orchestration hardening
**Goal:** true self-running portfolio.

- **Conductor:** full work graph (halt sequencing, cross-route ordering, commissioned
  producer/consumer, schema de-dup), **cross-week temperature state** (hot/warm/cold),
  multi-site prioritization + concurrency limits + shared-connector rate budgets, the
  data-not-instruction routing guard.
- **Report Generator:** the two-report flow (machine fixes report + human final report with
  the fixed spine and the honesty floor: no inflation, no buried failures, no invented
  numbers); GA4 trend via `ga4_report`; canonical `google_report`.
- **Deliverable / proof:** a full weekly portfolio run across Meridian (+ a second test site)
  that audits, fixes, verifies, carries outcome-pending items across weeks, and emits an
  honest final report.

---

## Connectors & cost (resolve as each phase needs them)
| Connector | Phase | Cost | Used by |
|---|---|---|---|
| WordPress REST + helper plugin | done | free | CMS adapter |
| Anthropic API | done | usage | every Claude call |
| Search Console (OAuth) | B | free | both auditors, ranking, cannibalization, coverage |
| PageSpeed / CrUX | B | free | CWV |
| GA4 | F | free | report trend |
| GBP API | E | free (Google) | Local Agent |
| Moz / Bing / Common Crawl | E | free tier | Off-page backlinks |
| DataForSEO (SERP/keyword/backlink/AI-visibility) | B-limited→E | paid | competitive groups, GEO live probe |

**Honest note:** competitive analysis (competitor keyword/link gaps), absolute search volume,
and live GEO probing genuinely need a paid source. Until then those groups run **limited**
and the report says so, rather than guessing.

## Sequencing rule
A phase starts only when the prior one is deployed and proven on Meridian in the browser —
the same gate that carried stages 0–6.
