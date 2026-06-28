# Ascend — Session Handoff & Status

Single source of truth for continuing this project in a fresh session. Read this
first, alongside `docs/agent-scopes.md` (the 11-agent blueprint) and
`docs/build-roadmap.md` (phased plan). Last updated end of the build session that
took the app from empty repo → self-running multi-agent SEO platform.

---

## 1. What this is
**Ascend** — a self-running, multi-agent SEO platform. You add a website, it gets
a private workspace, and a crew of Claude-powered agents audits it, fixes what
they can, and reports weekly. Owner is **non-coder** (sam@scaledai.org / "Farez")
and directs the build; do not assume coding knowledge. Built stage by stage from
`seo-agent-system-master-plan.md` (in the owner's Downloads), then expanded into
the full architecture in `docs/agent-scopes.md`.

## 2. Where everything lives
- **Local code:** `C:\Users\farez\Downloads\Claude Code\seo-agent`
- **GitHub:** `https://github.com/Farezomair/Claude-SEO-` (branch `main`). Push to
  `main` → Railway auto-deploys.
- **Railway:** project with two services — **Postgres** and the **Claude-SEO-**
  app. **App URL: https://claude-seo-production-cff6.up.railway.app**
- **Stack:** Python 3.12 · FastAPI + Jinja2 (server-rendered) · SQLAlchemy +
  Postgres (SQLite fallback for local dev). No build step.
- **Test site:** `meridianoutdoorkitchens.com` — WordPress + **Elementor** (page
  builder) + **Yoast SEO** + Google Site Kit. Local outdoor-kitchen business (not
  YMYL).
- **Sibling repo (reference only):** `C:\Users\farez\Downloads\Claude Code\claude-seo`
  — 25 SEO skills + ~50 scripts the blueprint maps agents onto. NOT our app.

## 3. Connections & secrets (all in Railway env vars; never in code/chat)
| Var | Purpose |
|---|---|
| `SECRET_KEY` | session cookie + encryption key derivation |
| `APP_USERNAME` / `APP_PASSWORD` | single-owner login |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `ANTHROPIC_API_KEY` | Claude (the brain). `ANTHROPIC_MODEL` optional (default `claude-opus-4-8`). |
| `WORDPRESS_URL` / `WORDPRESS_USERNAME` / `WORDPRESS_APP_PASSWORD` | Meridian fallback connection (per-site connections in the Settings tab override this; stored encrypted) |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | Search Console OAuth. Redirect = `https://claude-seo-production-cff6.up.railway.app/google/callback` |
| `CONTENT_PUBLISH_STATUS` | optional; `draft` (default) or `publish` for approved content |
| `WEEKLY_ENABLED` | `false` disables the scheduler (set locally for tests) |

- **Helper plugin** installed on Meridian: **"SEO Agent Bridge 2"** (slug
  `seo-agent-bridge-2`), v1.1. Source: `wordpress-plugin/seo-agent-connector.php`.
  Exposes Yoast meta to REST + a custom-CSS endpoint (`/wp-json/seo-agent/v1/custom-css`).
- **Google connected** ✅ (owner did the OAuth setup; consent screen published).

## 4. Code map (`app/`)
- `main.py` — FastAPI app, ALL routes + wiring, the `badge` Jinja filter, Google
  OAuth routes. Startup: `create_all` + `ensure_columns` + `start_scheduler`.
- `models.py` — `Site, Audit, Finding, FixRecord, JobRun, Approval, Content,
  SiteChange, SiteConnection, Rulebook, Report, RunLog, GoogleAuth`. (Legacy
  `AuditIssue`, `Fix` still defined but UNUSED — superseded by Finding/FixRecord.)
- `database.py`, `migrations.py` (`ensure_columns` — additive ALTER for new cols
  on existing tables; new tables come from `create_all`), `crypto.py` (Fernet from
  SECRET_KEY).
- `auth.py`, `connections.py` (resolve a site's WP connection: DB first, env
  fallback only when host matches), `rules.py` (editable agent rulebooks).
- **Auditor:** `crawler.py` (the Website Auditor crawl battery), `routing.py`
  (category → group/route/action_class), `gsc.py` + `google_oauth.py` (Search
  Console), `jobs.py` (`_run_audit` = crawl + GSC → routed Findings).
- **Doers:** `seo_technical.py` (`run_metafix` auto; `run_dedupe_titles` gated),
  `onpage_agent.py` (`run_meta_rewrites` — GSC ranking, gated), `website_agent.py`
  (`run_change` CSS; `run_page_drafts` missing pages), `content_agent.py`
  (`run_draft` blog posts), `content_corrector.py` (`run_correction` clean posts),
  `content_standard.py` (writing standard: BANNED words + em-dash scan/strip).
- `brain.py` — all Claude calls: `generate_meta`, `generate_article`,
  `generate_page`, `generate_css`, `correct_content`, `improve_meta`.
  `WRITING_STANDARD` injected into content prompts.
- `wordpress.py` — REST adapter: Yoast meta read/write, create post/page,
  update_content, custom CSS get/update, `list_content`.
- **Orchestration:** `weekly.py` (Conductor weekly loop: audit → metafix → report,
  the 7-phase skeleton), `scheduler.py` (in-process daemon timer).
- `templates/` — `base.html` (Ascend theme: aurora bg, Space Grotesk + Plus
  Jakarta, badges), `login`, `sites`, `site_detail` (tabs: Audit/Fixes/Content/
  Website/Reports/Settings), `approvals`, `rules`.

**Key enums:** Approval `kind` ∈ {content, required_page, content_fix,
website_css, meta_rewrite}. JobRun `kind` ∈ {metafix, dedupe, onpage, pagedraft,
contentfix, content_draft, website, weekly}.

## 5. What's BUILT (the architecture, in reality)
Everything rides the **Finding → FixRecord** data model (Phase A). Findings are
routed/classified/severity-tagged/verified.

**Auditor (detection) — comprehensive on free + GSC data:**
- Crawl: broken pages/links (bot-block aware), redirects, orphan pages (only when
  full crawl), HTTPS/mixed-content/security-headers, robots/sitemap/noindex/
  canonical, titles (missing/duplicate), H1, viewport, favicon, alt, Open Graph,
  header/footer, required pages, thin content, schema validity (deprecated
  FAQPage/HowTo, placeholders).
- Search Console: striking-distance queries (pos 5–20), low-CTR pages.

**Doers — what actually changes the live site (all gated except metas):**
| Doer | Built | Action |
|---|---|---|
| SEO Technical | meta fixes (**auto**), duplicate-title fixes (gated) | Yoast title/desc write |
| SEO On-page | rewrite title/meta for GSC ranking opps (gated) | Yoast write |
| Website Agent | CSS changes (gated, revertible), draft missing pages (gated) | custom CSS / create WP page |
| Content Writer | draft blog posts (gated) | create WP post draft |
| Content Corrector | clean blog posts to writing standard (gated) | update post content |
| SEO Off-page | **NOT BUILT** | needs backlink source |
| Local Agent | **NOT BUILT** | needs GBP API connector |

**Safety:** approval gate (Approvals screen, before/after previews), snapshot +
rollback (CSS has one-click Revert), verification (re-check before closing).
**Self-running:** weekly scheduler (audit → metafix → report). **Tuning:** Rules
page. **Reports:** weekly summary.

## 6. Build history (done)
Original stages 0–6 (empty shell → auditor → meta fixer → approvals+content →
weekly scheduler → rulebooks+SEO-backend checks → CSS website agent). Rebrand to
**Ascend**. Then: **Phase A** (Finding/Fix model), **Phase B** (auditor battery
expansion + thin-content/schema + OG/orphan + Google OAuth/GSC), **Phase C**
(missing-page drafts, GSC ranking rewrites, duplicate-title fixer), **Phase D**
(content team + mechanical writing standard).

## 7. The capability walls (honest) + THE KEY INSIGHT
Detection-only today (no fix path yet): security headers, redirects, schema
injection/removal, Elementor page bodies, CWV, backlinks, local/GBP, competitor
data.

**KEY INSIGHT (the agreed next direction):** the WordPress-REST "wall" is SOFTER
than first stated. The default REST API is limited, but **a custom helper plugin
(we already use one) can expose almost anything WordPress/PHP can do** via custom
REST endpoints. Re-categorized honestly:
- **✅ Achievable by extending the helper plugin:** security headers (`send_headers`
  hook), redirects (option + `template_redirect`), removing FAQPage / injecting
  JSON-LD schema (`wpseo_schema_graph` filter / `wp_head`). Canonicals/noindex
  already writable via Yoast meta.
- **⚠️ Hard/fragile:** Elementor page bodies (parse `_elementor_data` widget JSON
  — risky), CWV (overlaps optimization plugins).
- **❌ Not WordPress at all (REST can't help):** backlinks/off-page, GBP/local,
  competitor data — need external API connectors.

## 8. >>> ARCHITECTURE UPDATE (latest session — read this first) <<<
**The helper-plugin-v1.2 plan below is SUPERSEDED.** We found a far better,
fully-headless execution layer already live on the site: the **official WordPress
Abilities API** (`wp-abilities/v1`) + MCP Adapter, authenticated with the
Application Password we already store. No third-party relay, no interactive OAuth.
(WPVibe/"Vibe AI" was evaluated and rejected: interactive-OAuth + hosted relay
only — useless for an unattended app. See memory `ascend-execution-layer`.)

- **New code:** `abilities.py` (`AbilitiesClient`: `read`/`run`; GET for read-only
  abilities with PHP bracket-notation params, POST for writes), `elementor_agent.py`
  (the flagship doer). Discovery routes in `main.py`: `GET /sites/{id}/abilities`
  (catalog) and `GET /sites/{id}/elementor-probe` (page widget shapes). Public
  `GET /version` returns the `BUILD` marker to confirm a Railway deploy is live.
- **Catalog:** 111 abilities — full reference in `docs/abilities-catalog.md`.
  Reach now includes Elementor page editing, alt text, menus/internal links,
  LiteSpeed presets (CWV), site settings, and `plugin-install` (self-provisioning).
- **Page architecture (important):** every Meridian page is ONE Elementor `html`
  widget holding a full hand-built HTML document (likely AI-builder output); there
  are no separate heading/text widgets. Pages are already fairly SEO-strong.
- **DONE this session — Elementor On-page agent:** full-page SEO rewrite, gated,
  with a live visual preview (sandboxed iframe in Approvals), safety checks
  (truncation / lost style·script·links·images / banned words), a saved snapshot +
  one-click Revert, and verify-after-write. `brain.rewrite_page_html` STREAMS (the
  non-streaming call hit the SDK's 10-min guard). Write path: `apply_html` tries
  `elementor-update-widget-content`, falls back to a surgical `_elementor_data`
  edit. Trigger: Website tab → "Rewrite for SEO" per page (`JobRun` kind
  `elementor`, Approval kind `page_rewrite`). PROVEN end-to-end on Grill Repair.

**NEXT STEP options (pick with owner):** (a) make it self-running — wire rewrites
into the weekly Conductor loop + a "rewrite all pages" batch + fix the
auto-refresh-to-completion UX; (b) more Abilities doers — alt text at scale,
internal-link insertion between service/location pages, LiteSpeed CWV preset,
redirects via self-installed Redirection plugin; (c) add a copy-only diff view to
the approval; (d) consolidate/retire the now-partly-redundant `seo-agent-connector`
helper plugin.

---
### (historical, SUPERSEDED) original v1.2 plan
**Extend the helper plugin to v1.2** to unlock category ✅, then build the doers
that use it:
1. Add REST endpoints to `wordpress-plugin/seo-agent-connector.php`:
   - `POST /wp-json/seo-agent/v1/security-headers` — store which headers to send;
     a `send_headers` hook emits them. (GET to read current.)
   - `GET/POST /wp-json/seo-agent/v1/redirects` — manage simple 301 rules (store
     in an option, apply on `template_redirect`).
   - `POST /wp-json/seo-agent/v1/schema` — toggle/strip schema types (filter
     `wpseo_schema_graph` to remove FAQPage) or inject JSON-LD.
   All gated by `current_user_can('manage_options')`/`edit_theme_options`.
2. Rebuild the plugin zip with **Python `zipfile` + forward-slash arcnames**
   (Windows `Compress-Archive` breaks WP upload — see Gotchas). Owner re-uploads
   once (give it a fresh slug if WP complains, e.g. `seo-agent-bridge-3`).
3. Add `wordpress.py` methods + doers: Website Agent applies security headers
   (gated); SEO Technical owns redirects + schema cleanup (gated). Wire to the
   existing `security_headers`, `redirect_issue`, `schema_deprecated` Findings
   (routing already exists). Reuse the approval/FixRecord pattern.

Then remaining options: more SEO On-page battery (headings, internal links, AEO/
GEO formatting), Content Writer depth expansion, auto-dispatch in the weekly loop,
Phase F (two-part honest Report Generator), or new connectors (backlink source →
Off-page; GBP API → Local).

## 8c. LATEST SESSION (paused — resume here tomorrow)
Big build session on top of the Abilities execution layer. All live on Railway
(`/version` returns the current BUILD marker — last shipped: **`sweep-fixes-16`**).

**Shipped this session:**
- **Command Center** (new DEFAULT tab): left "crew" sidebar (Auditors/Doers/Reports),
  centre pipeline Audit→Route→Fix→Report with live animated progress bars (JS polls
  `GET /sites/{id}/pipeline-status`), SEO-health score ring + category bars + top
  priorities, "Start full run" + "Apply fixes" buttons. The other tabs still exist
  (Audit/Fixes/Content/Website/Reports/Settings) — owner WANTS them collapsed into a
  quiet left-nav (the UI-restructure task, NOT yet done).
- **Rebuilt Auditor** (`scoring.py` + expanded `crawler.py` + `content_analyzer.py`
  + `perf.py`): 0–100 health score, A–F grade, per-category scores, impact×effort
  prioritized fix plan. Phase-2 specialists: Claude E-E-A-T/GEO judgement on a few
  pages, real Core Web Vitals via free PageSpeed/CrUX (keyless or PAGESPEED_API_KEY).
- **Dispatcher** (`dispatcher.py`): routes open findings to doers — SAFE auto-applied
  (metas, now LiteSpeed-cache-flushed, cap 25), RISKY → Approvals (dedupe titles,
  missing pages, schema). Wired into the weekly Fix phase + the "Apply fixes" button.
- **New doer:** Organization/LocalBusiness **schema injection** (`schema_agent.py` +
  `brain.generate_schema_jsonld`) — generates entity JSON-LD from real homepage facts,
  appends to the homepage Elementor html widget, gated + verified + revertible.
- **Skill knowledge harvested** into `knowledge.py` (META/EEAT/GEO/SCHEMA/IMAGE guides
  from the ~/.claude/skills SEO skills) and injected into doer prompts (Elementor
  rewrite, content analyzer, meta gens, schema gen). The Railway app CANNOT run the
  skills at runtime — we transplant their rules into prompts. See memory
  [[ascend-audit-rebuild]] and [[ascend-execution-layer]].
- **Reports:** "Progress over time" chart (Issues ↓ / Fixes / Health ↑), all reports
  listed with **PKT (GMT+5)** timestamps via the `pkt` Jinja filter.
- **Bug fixes:** required-page presence uses 200-OK pages not just links (privacy was
  linked-but-404 → now drafted); duplicate approvals deduped + collapsed; stale-job
  guard for fix/weekly runs.

**Multi-agent defect sweep (ran via Workflow, 108 findings, verified).** Full report:
task output `…/tasks/wj88pyz5h.output`. FIXED the top set: #2 finding-lifecycle
stall (each audit now retires prior findings — was the root cause of "reports stay
the same"), #1 dead Content Writer route, #4 cache-flush after ALL gated writes,
#3 verify meta description, #14 XSS `|sanitize` filter, #13 SameSite+Secure cookie,
#9/#10 crash guards. **Remaining backlog (not done):** full CSRF tokens, double-approve
idempotency lock, SSRF private-IP filter; detection false-positives (header/footer on
Elementor, thin-content counting `<script>`, GSC position=0, mixed-content); scoring
penalty saturation (`scoring.py:73`); UI polish (findings filter/pagination, chart
axes, ARIA, editable schedule).

**>>> RESUME TOMORROW — owner to pick:** (a) keep knocking down the sweep backlog
(detection accuracy + scoring + security), (b) build the **alt-text doer** (runner-up;
add to dispatcher using `knowledge.IMAGE_GUIDE`), (c) the **UI restructure** (Command
Center primary, tabs → left-nav), or (d) just re-test (Run audit → Apply fixes) now
that the loop-stall + cache bugs are fixed and watch the chart move.

## 9. Operational details (local dev / test / deploy)
- **Real Python:** `C:\Users\farez\AppData\Local\Programs\Python\Python312\python.exe`
  (the WindowsApps `python.exe` is a fake stub — see memory `python-environment`).
- **Run locally:** set `APP_PASSWORD`, `SECRET_KEY`, `WEEKLY_ENABLED=false`, then
  `python -m uvicorn app.main:app --host 127.0.0.1 --port <N>`. Use **`curl.exe`**
  (PowerShell `curl` is an alias for Invoke-WebRequest and won't take `--header`).
  Stop a local server via PowerShell `Stop-Process` (filter CommandLine for
  `uvicorn app.main`).
- **Audits are slow** (~70–130s; crawl cap MAX_PAGES=30). Doers that call Claude
  need `ANTHROPIC_API_KEY`; locally they fail gracefully (logged, no crash) — full
  happy paths verify on Railway.
- **Deploy:** commit + `git push origin main` → Railway auto-deploys (~1–2 min).
  New DB tables auto-create on startup; `ensure_columns` adds new columns to
  existing tables. End commit messages with the Co-Authored-By trailer.
- **Verify discipline:** every change should be verified in the browser on Railway
  before moving on (the "ship + prove on Meridian" rule).

## 10. Gotchas / conventions
- **Plugin zips:** Windows `Compress-Archive` produces zips WordPress rejects
  ("Plugin file does not exist") and uploading over an existing slug errors —
  build with Python `zipfile` writing `slug/slug.php` (forward slashes), and use a
  fresh slug to avoid stale-folder collisions.
- **Secrets:** never in chat/code/git. Owner once leaked an API key in chat (it was
  rotated). Service-account/Google-Cloud setup was painful for the owner — prefer
  OAuth one-click; owner declined paid data sources.
- **Elementor:** Meridian's service pages are Elementor — their visible body is NOT
  the WP `content` field, so content edits don't show; Yoast META still writable.
- **Writing standard:** content + report agents must produce zero banned terms /
  zero em dashes (`content_standard.py`). Honor it in any content the agents emit.
- **Honesty:** the owner values straight talk about what is/ isn't achievable —
  build only what actually works, flag walls plainly, never fake coverage.
