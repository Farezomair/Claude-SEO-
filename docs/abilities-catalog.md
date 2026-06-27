# Meridian Abilities Catalog (discovered)

Live reach of the headless **Abilities API** (`wp-abilities/v1`) on Meridian, read
with Ascend's stored Application Password (no relay, no human). 111 abilities.
Run an ability: `POST /wp-json/wp-abilities/v1/abilities/{name}/run` body `{"input": {...}}`.

Providers: `hostinger-ai-assistant/*` (the rich set), `core/*` (WP core),
`yoast-seo/*`, `wpforms/*`. Names below omit obvious CRUD twins for brevity.

## SEO-relevant abilities we will use

### Meta / content (already do via REST — now also here)
- `…/posts-update`, `…/pages-update` — full update incl. `meta` (Yoast title/desc,
  noindex/nofollow, focuskw) AND `_elementor_data`. Required: `id`.
- `…/posts-search`, `…/pages-search`, `…/cpt-search` — find content.
- `…/posts-get`, `…/pages-get` — full object.

### Elementor (CRACKS THE BIGGEST WALL — Meridian service pages are Elementor)
- `…/elementor-get-page-structure` — readable tree of a page's containers/widgets.
- `…/elementor-find-widgets` — locate widgets by type (heading, button, image, …).
- `…/elementor-get-widget-by-id` — full widget settings.
- `…/elementor-update-widget-content` — edit heading/button/text-editor TEXT safely.
- `…/elementor-update-widget-styles` / `…-edit-container` — colors, typography, spacing.
- `…/elementor-update-widget-image` — image src + **alt text**.
- `…/elementor-update-widget-link` — button/link URLs (+ nofollow, target).
- `…/elementor-update-global-styles`, `…-assign-global-color`, `…-get-active-kit`.
- `…/elementor-create-container`, `…-delete-container`, `…-update-container`.

### Images / alt text (fixes images_missing_alt at scale)
- `…/media-update` — sets `alt_text`, caption, description. Required: `id`.
- `…/media-search`, `…/media-list`, `…/media-get`.

### Navigation / internal links (fixes orphan pages, nav)
- `…/menus-*`, `…/menu-items-*`, `…/menu-locations-*`.

### Performance / CWV
- `…/litespeed-cache-preset` (basic→extreme, auto-backup), `…-flush`, `…-settings-get`.

### Site settings
- `…/wp-settings-get`, `…/wp-settings-update` (title, tagline, front page, …).
- `core/get-site-info`, `core/get-environment-info` (WP/PHP version).

### Yoast read signals
- `yoast-seo/get-seo-scores`, `yoast-seo/get-readability-scores` (recent posts).

### Self-provisioning (lets Ascend install its own tools)
- `…/plugin-install` (WP.org slug), `…/plugin-activate`, `…/plugin-update`,
  `…/plugins-info`. → e.g. install `redirection` for 301s, or a headers plugin.
- `…/theme-*`, `…/template-part-*`, `…/revisions-*` (incl. `revisions-restore`).

## The three original gaps — re-mapped honestly
- **Schema (strip deprecated FAQPage/HowTo):** ✅ now fixable at the SOURCE — find
  & edit/remove the FAQ block via Elementor abilities or `pages-update` content,
  instead of a risky schema-graph filter. No raw PHP.
- **Redirects (301/302 cleanup):** ⚠️→✅ no native redirect ability, but Ascend can
  `plugin-install` the battle-tested **Redirection** plugin and drive its REST API.
- **Security headers:** ⚠️ still no native ability. Best path: install a headers
  plugin via `plugin-install`, or set at host/CDN. Smallest-value of the three.

## Safety
Destructive abilities exist (`*-delete` for posts/pages/users/plugins,
`theme-deactivate`, `plugin-delete`). Ascend must use a strict **allowlist** of
safe abilities and keep the approval gate for anything that changes the live site.
Revisions + LiteSpeed presets auto-backup, giving rollback paths.
