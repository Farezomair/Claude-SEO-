# Ascend — Session Handoff & Status

Single source of truth for continuing this project in a fresh session. Read this
first, alongside `docs/agent-scopes.md` (the 11-agent blueprint) and
`docs/build-roadmap.md` (phased plan). Last updated end of the build session that
took the app from empty repo → self-running multi-agent SEO platform.

---

## 0. CURRENT STATE — READ THIS FIRST (supersedes older sections below)

**Ascend** = a self-running, multi-agent SEO platform. Owner is a **non-coder**
(sam@scaledai.org / "Farez") who directs the build — explain plainly, give straight
talk, build only what actually works. Goal: it audits a site, fixes what it safely
can, sends risky changes to an approval gate, and keeps a score climbing.

### Where it lives
- **Live app:** https://claude-seo-production-cff6.up.railway.app
- **Repo:** `https://github.com/Farezomair/Claude-SEO-` (branch `main`; push → Railway auto-deploys ~1-2 min)
- **Local code:** `C:\Users\farez\Downloads\Claude Code\seo-agent`
- **Test site:** meridianoutdoorkitchens.com (WordPress + **Elementor** + Yoast on **Hostinger**; Google Search Console connected). Site #1 in the app.
- **Confirm a deploy is live:** `GET /version` returns `{"build": "<marker>"}`. **Latest marker: `writepath-publish-amend-26`.** Bump the `BUILD` constant in `main.py` every deploy and poll `/version?x=N` (cache-bust) to confirm before telling the owner to test.

> **LATEST SESSION (write-path hardening + richer approvals).** Adversarially audited the Elementor write/verify/revert/approve path (Workflow, 37 candidates → 16 confirmed bugs) and fixed all 16 + 9 review follow-ups. The big one: `elementor-update-widget-content` **silently no-ops on Meridian's html widgets** (returns 200, changes nothing) and the code treated 200 as success — so approved writes never landed yet were reported "verified". Now `apply_html` self-heals (verifies the write landed; falls through to the `_elementor_data` surgical edit when it didn't), and `verify_change()` re-reads the live widget and checks the ACTUAL change (markers), not the first 400 chars. Revert/compose fixed (re-reads live, true revert snapshot, image-dims/schema compose instead of clobber). Schema dup-guard now parses JSON-LD + recognizes LocalBusiness subtypes; **crawler detector** (`crawler.py` `LOCALBUSINESS_SUBTYPES`) agrees so the schema finding actually resolves. **New approval actions** (owner ask): *Approve & publish live* vs *Approve & save as draft* (drafts don't move the score — they re-surface next audit), *Request amendment* (inline box → `app/amend.py` regenerates in the background; `brain.py` generators take an `instructions=` arg), and approvals act in place from the Command Center (`return_to`). Added `Approval.amend_note` column. **STILL UNPROVEN:** an approved Elementor write landing end-to-end on Meridian — the code is now correct + self-verifying, but the owner needs to approve one live (e.g. image-dims) and confirm the page still renders.

### Architecture as it stands now
- **Single-screen Command Center** is the WHOLE app (`templates/site_detail.html`). The site route forces `tab="command"`; the old tabs are gone. Layout: header (pretty site name + last-audited PKT) + action bar at top (**Run audit & fix** = `run-weekly`, **Apply fixes** = `run-fixes`, **Stop** = `stop-run`) → **clickable/expandable Pipeline** (Audit→Route→Fix→Report; click a stage for live detail; the Fix stage shows N/total + a live per-finding log) → **SEO Health** ring + grade + category bars + a 1-2 para **AI narrative** → expandable panels: **Full report** (prioritized plan + progress chart + findings with a Result column), **Approvals** (Needs-your-attention human tasks + proposals with previews), **Settings** (WP connection + Google + schedule). A JS poller hits `GET /sites/{id}/pipeline-status` every 1.5s and reloads the page once a run finishes. Container is `.container.wide`.
- **Execution layer = the site's official WordPress Abilities API** (`wp-abilities/v1` + `/mcp`), driven HEADLESS with the stored Application Password (no relay, no OAuth). `abilities.py::AbilitiesClient` — `read()`=GET with PHP bracket-notation params, `run()`=POST `{"input":{}}` with a 405→GET fallback. 111 abilities live (Elementor editing, media, menus, LiteSpeed, plugin-install, …) — full list in `docs/abilities-catalog.md`. **WPVibe was rejected** (interactive OAuth + hosted relay) and the **custom helper-plugin v1.2 plan was dropped** in favor of this. See memories [[ascend-execution-layer]] and [[ascend-audit-rebuild]].
- **Scored auditor:** `jobs._run_audit` = crawl (`crawler.py`) + Search Console (`gsc.py`) + Claude content/E-E-A-T/GEO (`content_analyzer.py` + `brain.analyze_page_content`, now 2 pages × ≤3 findings for stability) + Core Web Vitals via free PageSpeed/CrUX (`perf.py`). `scoring.py` → 0-100 health + A-F grade + per-category scores + an impact×effort prioritized roadmap. `brain.summarize_health` → the narrative. All stored on `Audit` (health_score/grade/category_scores/roadmap/narrative). **Each audit SUPERSEDES the prior findings** (fresh source of truth; no accumulation).
- **Dispatcher (`dispatcher.py`) = per-finding engine.** Walks each open finding (severity order). Per finding, a handler does ONE of: **FIX** (meta title/desc via Yoast — applied inline + verified + cache-flushed), **PROPOSE** (page rewrites/images/schema generate in the BACKGROUND → land in Approvals; missing pages/dedupe/ranking generated inline → Approvals), **HUMAN** (`needs_real_data` → "Needs your attention"), or **NO-CAPABILITY** (honest remark naming what's needed). Writes `status` + `remark` on each finding (shown in the Result column + the live Fix log). Heavy generators are BACKGROUNDED so the bar never stalls. Caps: `MAX_AUTO_FIXES=25`, `MAX_REWRITES=2`. Runs from the weekly Fix phase AND the "Apply fixes" button. **Stop** sets runs to `cancelled`; the loop re-checks and halts.
- **Doers:** SEO Technical (`seo_technical.py` — meta auto-fix + cache flush; dedupe gated), SEO On-page (`onpage_agent.py` ranking rewrites; `elementor_agent.py` full-page rewrite, gated, cache-flush, revert), Website Agent (`website_agent.py` CSS + missing-page drafts; `image_agent.py` image dimensions, gated), Content Corrector (`content_corrector.py`), Schema (`schema_agent.py` Organization/LocalBusiness JSON-LD injection, gated), Report Generator (`weekly.py`). Skill expertise harvested into `knowledge.py` (META/EEAT/GEO/SCHEMA/IMAGE guides) and injected into `brain.py` prompts (the deployed app CANNOT call the `~/.claude/skills` skills at runtime — we transplant their rules).
- **Safety:** gated Approvals for risky writes; LiteSpeed cache flush after gated writes; one-click revert for website_css/page_rewrite/schema_inject/img_dims; `|sanitize` Jinja filter on AI/DB HTML; SameSite+Secure session cookie (`COOKIE_INSECURE=1` for local http); 180s Claude per-call timeout; 20-min stale-job guard; Stop button.

### >>> OPEN ISSUES — verify/finish these first <<<
1. **The live WRITE path is CODE-FIXED but not yet PROVEN end-to-end on Meridian.** Root cause found + fixed this session: `elementor-update-widget-content` no-ops on these single-HTML-widget pages, and the old code believed the 200 and lied "verified". `apply_html` now self-heals to the `_elementor_data` path and `verify_change` re-reads the live page, so a write either truly lands (and is honestly verified) or is reported failed. **NEXT (the actual proof): approve ONE low-risk proposal — the image-dimensions one — with "Approve & apply live", then confirm on the live page that the image has width/height and the page still renders.** Watch the Fixes log: it now says "verified live" only when the change is really on the page. If it still doesn't land → the `_elementor_data` surgical edit needs inspection on a real Meridian page (use `GET /sites/{id}/elementor-probe`).
2. **On-page score = 0 and "0 fixes applied".** Was partly the heading-skip flood (now collapsed to ONE finding in the crawler). Remaining On-page=0 likely = per-page `meta_description_missing` that the meta auto-fixer should fix but reported 0 — a URL-path fallback for matching was added; **verify metas actually match a WP page and write** (or Yoast metas are already set and the 0 is other on-page findings the rewrites fix once approved).
3. The score was **dropping/volatile each run** — mitigated (analyzer trimmed to 2 pages × 3 findings, heading flood collapsed). Confirm it's stable now.
4. The pipeline **stalling** ("stuck at 24/60") — fixed by backgrounding heavy generators + 180s timeout + Stop. Confirm a fresh run reaches **Report** fast.

### Backlog (from the 108-finding multi-agent defect sweep + roadmap)
- **Build remaining doers:** FAQPage/HowTo schema-cleanup (strip deprecated); favicon (`wp-settings` site_icon); redirects (self-install the Redirection plugin via `plugin-install`); security headers / llms.txt (host/CDN-level, low value).
- **Business Profile:** a one-time form for the owner's real facts (phone, address, license #, hours, prices, owner name) so doers inject REAL data — auto-resolves the `needs_real_data` human tasks.
- **Security hardening (sweep):** full CSRF tokens (only SameSite cookie done); double-approve idempotency lock; SSRF private-IP filter in the crawler/fetchers.
- **Detection accuracy (sweep):** header/footer false-positives on Elementor; thin-content counting `<script>` text; GSC `position=0` garbage; mixed-content false positives.
- **Scoring:** penalty saturation in `scoring.py` (a count-aware curve so 2 vs 100 issues differ).
- **UX:** approving from the embedded panel currently redirects to the standalone `/approvals` page (make it return to the Command Center); chart axes/labels; findings filter/pagination.
- **Self-running loop:** the weekly scheduler already chains audit→dispatch→report; verify it and decide the auto-apply policy.

### Operational
- **Real Python:** `C:\Users\farez\AppData\Local\Programs\Python\Python312\python.exe` (WindowsApps python.exe is a fake stub — memory [[python-environment]]).
- **Local run:** `COOKIE_INSECURE=1 WEEKLY_ENABLED=false SECRET_KEY=x APP_PASSWORD=x python -m uvicorn app.main:app --port <N>`. Import smoke-test + render templates with `templates.get_template(...).render(...)` before deploying.
- **Deploy:** commit + `git push origin main`; new tables via `create_all`, new columns via `migrations.ensure_columns`; end commit messages with the Co-Authored-By trailer.
- **Times** shown in **PKT (GMT+5)** via the `pkt` Jinja filter.
- **Approval kinds:** content, required_page, content_fix, website_css, meta_rewrite, page_rewrite, schema_inject, img_dims. **Finding statuses:** open, in-progress, closed, reopened, escalated, no-capability, needs-human, snoozed, superseded.

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
