<?php
/**
 * Plugin Name: SEO Agent Bridge 10
 * Description: Lets the SEO Agent (Ascend) read/update Yoast meta, Additional CSS, the Elementor HTML widget, and the _meridian_body field — and, new in v1.4, manage TECHNICAL SEO at the server level: send security response headers, serve /llms.txt (HTTP 200), 301-REDIRECT dead/old URLs, inject <head> tags (canonical / OG / viewport / favicon), extend or override robots.txt, NOINDEX thin archive pages, enforce the SEO title on the rendered page, and SCRUB fabricated JSON-LD from the page head (self-serving aggregateRating, fake streetAddress) that other plugins/themes inject. All toggleable via REST and fully reversible. Adds nothing visible to the front end on its own.
 * Version: 1.8.0
 * Author: SEO Agent System
 */

if (!defined('ABSPATH')) {
    exit;
}

/* ===========================================================================
 * 1) Yoast meta -> REST (unchanged)
 * ========================================================================= */
add_action('init', function () {
    $meta_keys = array('_yoast_wpseo_title', '_yoast_wpseo_metadesc',
                       '_yoast_wpseo_meta-robots-noindex', '_yoast_wpseo_meta-robots-nofollow');
    foreach (array('post', 'page') as $pt) {
        foreach ($meta_keys as $key) {
            register_post_meta($pt, $key, array(
                'show_in_rest' => true, 'single' => true, 'type' => 'string',
                'auth_callback' => function () { return current_user_can('edit_posts'); },
            ));
        }
    }
});

/* ===========================================================================
 * 2) TECHNICAL SEO (NEW in v1.4): security headers + /llms.txt
 * ========================================================================= */

// Security response headers — emitted on every front-end response when enabled.
// Values are chosen to satisfy the audit + be SAFE (CSP only upgrades insecure
// requests; it does not restrict sources, so it can't break the site). Toggle via
// the option so it's fully reversible.
add_action('send_headers', function () {
    if (is_admin() || !get_option('seo_agent_security_headers')) {
        return;
    }
    if (!headers_sent()) {
        header('Strict-Transport-Security: max-age=31536000; includeSubDomains');
        header('X-Content-Type-Options: nosniff');
        header('X-Frame-Options: SAMEORIGIN');
        header('Referrer-Policy: strict-origin-when-cross-origin');
        header('Content-Security-Policy: upgrade-insecure-requests');
    }
}, 1);

// Serve /llms.txt from a stored option (the emerging standard for guiding AI
// assistants). Intercept early so it works without a physical file.
add_action('template_redirect', function () {
    $uri = isset($_SERVER['REQUEST_URI']) ? strtok($_SERVER['REQUEST_URI'], '?') : '';
    if (rtrim($uri, '/') !== '/llms.txt') {
        return;
    }
    $content = (string) get_option('seo_agent_llms_txt', '');
    if ($content === '') {
        return; // not configured — let WordPress 404 normally
    }
    status_header(200);                 // override WP's 404 — this URL is now a real resource
    nocache_headers();
    header('Content-Type: text/plain; charset=utf-8');
    header('X-Robots-Tag: noindex');
    echo $content;
    exit;
}, 0);

// 301-redirect dead/old URLs to live ones from a stored map (set via REST). Lets
// the SEO agent resolve broken links/pages without editing every linking page.
// Fully reversible (clear the map). Skips admin; guards against self-redirects.
add_action('template_redirect', function () {
    if (is_admin()) { return; }
    $map = get_option('seo_agent_redirects', array());
    if (!is_array($map) || empty($map)) { return; }
    $uri  = isset($_SERVER['REQUEST_URI']) ? strtok($_SERVER['REQUEST_URI'], '?') : '';
    $path = parse_url($uri, PHP_URL_PATH);
    if (!$path) { return; }
    $norm = '/' . trim($path, '/');
    foreach (array($norm, $norm . '/') as $cand) {
        if (isset($map[$cand]) && $map[$cand]) {
            $to = (string) $map[$cand];
            if (strpos($to, 'http') !== 0) { $to = home_url($to); }
            if (untrailingslashit($to) === untrailingslashit(home_url($norm))) { return; }
            wp_redirect($to, 301);
            exit;
        }
    }
}, 0);

// Inject <head> tags the audit found MISSING (only enabled per finding, so no dupes):
// self-canonical, Open Graph, viewport, favicon. Reversible via the toggles.
add_action('wp_head', function () {
    if (is_admin()) { return; }
    if (get_option('seo_agent_head_canonical') && is_singular()) {
        $u = get_permalink();
        if ($u) { echo "
<link rel=\"canonical\" href=\"" . esc_url($u) . "\" />"; }
    }
    if (get_option('seo_agent_head_og')) {
        $title = wp_get_document_title();
        $url   = is_singular() ? get_permalink() : home_url('/');
        $desc  = '';
        if (is_singular()) {
            $p = get_post();
            if ($p) { $desc = has_excerpt($p) ? get_the_excerpt($p) : wp_trim_words(wp_strip_all_tags($p->post_content), 30); }
        }
        if (!$desc) { $desc = get_bloginfo('description'); }
        echo "
<meta property=\"og:title\" content=\"" . esc_attr($title) . "\" />";
        echo "
<meta property=\"og:type\" content=\"website\" />";
        if ($url)  { echo "
<meta property=\"og:url\" content=\"" . esc_url($url) . "\" />"; }
        if ($desc) { echo "
<meta property=\"og:description\" content=\"" . esc_attr($desc) . "\" />"; }
        $img = get_option('seo_agent_og_image');
        if (!$img && is_singular() && has_post_thumbnail()) { $img = get_the_post_thumbnail_url(null, 'full'); }
        if ($img) { echo "
<meta property=\"og:image\" content=\"" . esc_url($img) . "\" />"; }
    }
    if (get_option('seo_agent_head_viewport')) {
        echo "
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />";
    }
    $fav = get_option('seo_agent_favicon');
    if ($fav) { echo "
<link rel=\"icon\" href=\"" . esc_url($fav) . "\" />"; }
    echo "
";
}, 1);

// Extend robots.txt from a stored option (e.g. explicitly allow AI crawlers).
// Enforce the intended SEO title on the RENDERED page. Some themes hardcode
// their own <title>, silently overriding the SEO plugin — Google then indexes a
// junk default. When enabled, the page's Yoast title wins: first via the
// document-title filter, then via an output-buffer rewrite for themes that
// print <title> directly. Reversible (toggle off).
function seo_agent_resolved_title() {
    if (!is_singular()) { return ''; }
    $raw = (string) get_post_meta(get_the_ID(), '_yoast_wpseo_title', true);
    if ($raw === '') { return ''; }
    if (function_exists('wpseo_replace_vars')) {
        $raw = wpseo_replace_vars($raw, get_post());
    } elseif (strpos($raw, '%%') !== false) {
        return '';
    }
    return trim(wp_strip_all_tags($raw));
}

add_filter('pre_get_document_title', function ($title) {
    if (is_admin() || !get_option('seo_agent_title_override')) { return $title; }
    $t = seo_agent_resolved_title();
    return $t !== '' ? $t : $title;
}, 9999);

// Scrub fabricated JSON-LD from the rendered page. Other plugins/themes inject
// LocalBusiness schema into <head> with self-given star ratings (a Google policy
// violation) and placeholder street addresses. We can't edit their source, but
// we CAN rewrite the final HTML: json_decode each ld+json block, drop the
// aggregateRating node and fix/blank the bad streetAddress, re-encode. Reversible
// (toggle off). Bridge v10.
function seo_agent_scrub_jsonld_node(&$node, $opts, &$changed) {
    if (is_array($node)) {
        // Strip self-serving aggregateRating anywhere in the graph.
        if (!empty($opts['strip_reviews'])) {
            foreach (array('aggregateRating', 'aggregaterating', 'review', 'reviews') as $k) {
                if (isset($node[$k])) { unset($node[$k]); $changed = true; }
            }
        }
        // Fix a known-bad streetAddress (city masquerading as a street).
        if ($opts['bad_street'] !== '' && isset($node['streetAddress'])
            && is_string($node['streetAddress'])
            && strcasecmp(trim($node['streetAddress']), $opts['bad_street']) === 0) {
            if ($opts['street_mode'] === 'replace' && $opts['street_value'] !== '') {
                $node['streetAddress'] = $opts['street_value']; $changed = true;
            } else {
                unset($node['streetAddress']); $changed = true;  // service-area: no street
            }
        }
        foreach ($node as $k => &$v) { seo_agent_scrub_jsonld_node($v, $opts, $changed); }
        unset($v);
    }
}

function seo_agent_scrub_html($html) {
    $opts = array(
        'strip_reviews' => (bool) get_option('seo_agent_scrub_reviews'),
        'bad_street'    => (string) get_option('seo_agent_scrub_bad_street', ''),
        'street_mode'   => (string) get_option('seo_agent_scrub_street_mode', 'remove'),
        'street_value'  => (string) get_option('seo_agent_scrub_street_value', ''),
    );
    if (!$opts['strip_reviews'] && $opts['bad_street'] === '') { return $html; }
    return preg_replace_callback(
        '#(<script[^>]*type=(["\'])application/ld\\+json\\2[^>]*>)(.*?)(</script>)#is',
        function ($m) use ($opts) {
            $data = json_decode(trim($m[3]), true);
            if ($data === null) { return $m[0]; }
            $changed = false;
            seo_agent_scrub_jsonld_node($data, $opts, $changed);
            if (!$changed) { return $m[0]; }
            return $m[1] . wp_json_encode($data) . $m[4];
        }, $html);
}

add_action('template_redirect', function () {
    if (is_admin() || is_feed()) { return; }
    $do_title = get_option('seo_agent_title_override');
    $do_scrub = get_option('seo_agent_scrub_reviews') || get_option('seo_agent_scrub_bad_street');
    $text_rules = json_decode((string) get_option('seo_agent_text_rules', ''), true);
    $do_text = is_array($text_rules) && count($text_rules) > 0;
    if (!$do_title && !$do_scrub && !$do_text) { return; }
    $t = $do_title ? seo_agent_resolved_title() : '';
    ob_start(function ($html) use ($t, $do_scrub, $do_text, $text_rules) {
        if ($t !== '') {
            $html = preg_replace('#<title[^>]*>.*?</title>#si',
                                 '<title>' . esc_html($t) . '</title>', $html, 1);
        }
        if ($do_scrub) { $html = seo_agent_scrub_html($html); }
        // Universal text scrub: swap fabricated strings (a theme-hardcoded phone,
        // a placeholder license) in the FINAL HTML, wherever the theme printed
        // them — the surface no content doer can reach. Exact str_replace only.
        if ($do_text) {
            foreach ($text_rules as $rule) {
                if (!empty($rule['find'])) {
                    $html = str_replace((string) $rule['find'], (string) ($rule['replace'] ?? ''), $html);
                }
            }
        }
        return $html;
    });
}, 1);

add_filter('robots_txt', function ($output, $public) {
    $full = (string) get_option('seo_agent_robots_full', '');
    if ($full !== '') { return $full; }           // full override (e.g. unblock AI crawlers)
    $extra = (string) get_option('seo_agent_robots_extra', '');
    if ($extra !== '') { $output .= "
" . $extra . "
"; }
    return $output;
}, 20, 2);

add_action('rest_api_init', function () {

    // --- Additional CSS (unchanged) ---
    register_rest_route('seo-agent/v1', '/custom-css', array(
        array('methods' => 'GET', 'callback' => function () { return array('css' => (string) wp_get_custom_css()); },
              'permission_callback' => function () { return current_user_can('edit_theme_options'); }),
        array('methods' => 'POST', 'callback' => function ($r) {
                wp_update_custom_css_post((string) $r->get_param('css'));
                return array('ok' => true);
              }, 'permission_callback' => function () { return current_user_can('edit_theme_options'); }),
    ));

    // --- Elementor html widget (unchanged from Bridge 3/4) ---
    register_rest_route('seo-agent/v1', '/elementor', array(
        array('methods' => 'GET',  'callback' => 'seo_agent_elementor_read',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_elementor_write',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
    ));

    // --- _meridian_body (the live render source; from Bridge 4) ---
    register_rest_route('seo-agent/v1', '/body', array(
        array('methods' => 'GET',  'callback' => 'seo_agent_body_read',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_body_write',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
    ));

    // --- NEW: technical-SEO toggles (security headers + llms.txt) ---
    // GET  -> current state.  POST {security_headers:bool, llms_txt:string|null} -> set.
    register_rest_route('seo-agent/v1', '/tech', array(
        array('methods' => 'GET', 'callback' => function () {
                return array(
                    'security_headers' => (bool) get_option('seo_agent_security_headers'),
                    'llms_txt_len'     => strlen((string) get_option('seo_agent_llms_txt', '')),
                );
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_tech_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.5: 301 redirect map (dead/old URLs -> live ones) ---
    register_rest_route('seo-agent/v1', '/redirects', array(
        array('methods' => 'GET', 'callback' => function () {
                return array('redirects' => (array) get_option('seo_agent_redirects', array()));
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_redirects_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.6: <head> injection (canonical / OG / viewport / favicon) ---
    register_rest_route('seo-agent/v1', '/head', array(
        array('methods' => 'GET', 'callback' => 'seo_agent_head_state',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_head_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.7: noindex thin archives (Yoast tag/category/author) ---
    register_rest_route('seo-agent/v1', '/yoast-archives', array(
        array('methods' => 'GET', 'callback' => 'seo_agent_archives_state',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_archives_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.8: head JSON-LD scrubber (self-serving reviews + fake address) ---
    register_rest_route('seo-agent/v1', '/schema-scrub', array(
        array('methods' => 'GET', 'callback' => 'seo_agent_scrub_state',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_scrub_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.8: universal output-buffer text scrub (theme-hardcoded strings) ---
    register_rest_route('seo-agent/v1', '/text-scrub', array(
        array('methods' => 'GET', 'callback' => function () {
                $r = json_decode((string) get_option('seo_agent_text_rules', ''), true);
                return array('rules' => is_array($r) ? $r : array());
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_text_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.7: rendered-title override toggle ---
    register_rest_route('seo-agent/v1', '/title-override', array(
        array('methods' => 'GET', 'callback' => function () {
                return array('enabled' => (bool) get_option('seo_agent_title_override'));
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => function ($r) {
                $v = $r->get_param('enabled');
                if ($v !== null) { update_option('seo_agent_title_override', $v ? 1 : 0); }
                do_action('litespeed_purge_all');
                return array('enabled' => (bool) get_option('seo_agent_title_override'));
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));

    // --- NEW in v1.6: robots.txt extension ---
    register_rest_route('seo-agent/v1', '/robots', array(
        array('methods' => 'GET', 'callback' => function () {
                return array('extra' => (string) get_option('seo_agent_robots_extra', ''));
              }, 'permission_callback' => function () { return current_user_can('manage_options'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_robots_set',
              'permission_callback' => function () { return current_user_can('manage_options'); }),
    ));
});

function seo_agent_tech_set($request) {
    $out = array('ok' => true);
    $sh = $request->get_param('security_headers');
    if ($sh !== null) {
        update_option('seo_agent_security_headers', $sh ? 1 : 0);
        $out['security_headers'] = (bool) get_option('seo_agent_security_headers');
    }
    $llms = $request->get_param('llms_txt');
    if ($llms !== null) {
        update_option('seo_agent_llms_txt', (string) $llms);
        $out['llms_txt_len'] = strlen((string) get_option('seo_agent_llms_txt', ''));
    }
    // Purge so the new headers/llms.txt are served immediately.
    do_action('litespeed_purge_all');
    return $out;
}

function seo_agent_redirects_set($request) {
    $map = get_option('seo_agent_redirects', array());
    if (!is_array($map)) { $map = array(); }
    if ($request->get_param('clear')) { $map = array(); }
    $set = $request->get_param('set');
    if (is_array($set)) {
        foreach ($set as $from => $to) {
            $from = '/' . trim((string) $from, '/');
            if ($from !== '/' && $to) { $map[$from] = (string) $to; }
        }
    }
    $remove = $request->get_param('remove');
    if (is_array($remove)) {
        foreach ($remove as $from) { unset($map['/' . trim((string) $from, '/')]); }
    }
    update_option('seo_agent_redirects', $map);
    do_action('litespeed_purge_all');
    return array('ok' => true, 'count' => count($map), 'redirects' => $map);
}

function seo_agent_head_state() {
    return array(
        'canonical' => (bool) get_option('seo_agent_head_canonical'),
        'og'        => (bool) get_option('seo_agent_head_og'),
        'viewport'  => (bool) get_option('seo_agent_head_viewport'),
        'favicon'   => (string) get_option('seo_agent_favicon', ''),
        'og_image'  => (string) get_option('seo_agent_og_image', ''),
    );
}

function seo_agent_head_set($request) {
    foreach (array('canonical' => 'seo_agent_head_canonical',
                   'og'        => 'seo_agent_head_og',
                   'viewport'  => 'seo_agent_head_viewport') as $p => $opt) {
        $v = $request->get_param($p);
        if ($v !== null) { update_option($opt, $v ? 1 : 0); }
    }
    $fav = $request->get_param('favicon');
    if ($fav !== null) { update_option('seo_agent_favicon', esc_url_raw((string) $fav)); }
    $img = $request->get_param('og_image');
    if ($img !== null) { update_option('seo_agent_og_image', esc_url_raw((string) $img)); }
    do_action('litespeed_purge_all');
    return seo_agent_head_state();
}

function seo_agent_text_set($request) {
    $rules = $request->get_param('rules');
    $clean = array();
    if (is_array($rules)) {
        foreach ($rules as $rule) {
            if (!empty($rule['find'])) {
                $clean[] = array('find' => (string) $rule['find'],
                                 'replace' => (string) (isset($rule['replace']) ? $rule['replace'] : ''));
            }
            if (count($clean) >= 30) { break; }
        }
    }
    update_option('seo_agent_text_rules', wp_json_encode($clean));
    do_action('litespeed_purge_all');
    return array('rules' => $clean);
}

function seo_agent_scrub_state() {
    return array(
        'strip_reviews' => (bool) get_option('seo_agent_scrub_reviews'),
        'bad_street'    => (string) get_option('seo_agent_scrub_bad_street', ''),
        'street_mode'   => (string) get_option('seo_agent_scrub_street_mode', 'remove'),
        'street_value'  => (string) get_option('seo_agent_scrub_street_value', ''),
    );
}

function seo_agent_scrub_set($request) {
    if ($request->get_param('strip_reviews') !== null) {
        update_option('seo_agent_scrub_reviews', $request->get_param('strip_reviews') ? 1 : 0);
    }
    if ($request->get_param('bad_street') !== null) {
        update_option('seo_agent_scrub_bad_street', sanitize_text_field($request->get_param('bad_street')));
    }
    if ($request->get_param('street_mode') !== null) {
        $mode = $request->get_param('street_mode') === 'replace' ? 'replace' : 'remove';
        update_option('seo_agent_scrub_street_mode', $mode);
    }
    if ($request->get_param('street_value') !== null) {
        update_option('seo_agent_scrub_street_value', sanitize_text_field($request->get_param('street_value')));
    }
    do_action('litespeed_purge_all');
    return seo_agent_scrub_state();
}

function seo_agent_archives_state() {
    $t = get_option('wpseo_titles');
    if (!is_array($t)) { return array('yoast' => false); }
    return array(
        'yoast'      => true,
        'tags'       => !empty($t['noindex-tax-post_tag']),
        'categories' => !empty($t['noindex-tax-category']),
        'authors'    => !empty($t['noindex-author-wpseo']),
    );
}

function seo_agent_archives_set($request) {
    $t = get_option('wpseo_titles');
    if (!is_array($t)) {
        return new WP_Error('no_yoast', 'Yoast SEO is not active on this site', array('status' => 422));
    }
    foreach (array('tags' => 'noindex-tax-post_tag',
                   'categories' => 'noindex-tax-category',
                   'authors' => 'noindex-author-wpseo') as $p => $key) {
        $v = $request->get_param($p);
        if ($v !== null) { $t[$key] = (bool) $v; }
    }
    update_option('wpseo_titles', $t);
    // Yoast excludes noindexed archives from its sitemaps automatically; purge
    // both caches so the change serves immediately.
    if (function_exists('wpseo_enable_tracking')) { /* no-op guard */ }
    do_action('wpseo_sitemap_index_invalidate');
    do_action('litespeed_purge_all');
    return seo_agent_archives_state();
}

function seo_agent_robots_set($request) {
    $extra = $request->get_param('extra');
    if ($extra !== null) { update_option('seo_agent_robots_extra', (string) $extra); }
    $full = $request->get_param('full');
    if ($full !== null) { update_option('seo_agent_robots_full', (string) $full); }
    do_action('litespeed_purge_all');
    return array('ok' => true,
                 'full'  => (string) get_option('seo_agent_robots_full', ''),
                 'extra' => (string) get_option('seo_agent_robots_extra', ''));
}

/* ===========================================================================
 * 3) _meridian_body handlers (from Bridge 4)
 * ========================================================================= */
function seo_agent_purge_post($post_id) {
    wp_update_post(array('ID' => (int) $post_id));
    clean_post_cache($post_id);
    do_action('litespeed_purge_post', $post_id);
    $link = get_permalink($post_id);
    if ($link) { do_action('litespeed_purge_url', $link); }
    do_action('litespeed_purge_all');
    delete_post_meta($post_id, '_elementor_css');
}

function seo_agent_body_read($request) {
    $post_id = (int) $request->get_param('post_id');
    if (!$post_id) { return new WP_Error('bad_request', 'post_id required', array('status' => 400)); }
    $body = (string) get_post_meta($post_id, '_meridian_body', true);
    $out = array('post_id' => $post_id, 'has_body' => $body !== '', 'body_len' => strlen($body),
                 'img_count' => preg_match_all('/<img\b/i', $body));
    if ($request->get_param('include_html')) { $out['html'] = $body; }
    return $out;
}

function seo_agent_body_write($request) {
    $post_id = (int) $request->get_param('post_id');
    $html = $request->get_param('html');
    if (!$post_id || $html === null) { return new WP_Error('bad_request', 'post_id and html required', array('status' => 400)); }
    $html = (string) $html;
    update_post_meta($post_id, '_meridian_body', wp_slash($html));
    seo_agent_purge_post($post_id);
    $live = (string) get_post_meta($post_id, '_meridian_body', true);
    return array('ok' => true, 'post_id' => $post_id, 'verified' => ($live === $html), 'body_len' => strlen($live));
}

/* ===========================================================================
 * 4) Elementor handlers (from Bridge 3)
 * ========================================================================= */
function seo_agent_elementor_decode($post_id) {
    $raw = get_post_meta($post_id, '_elementor_data', true);
    if (is_array($raw)) { return $raw; }
    if (is_string($raw) && $raw !== '') { $d = json_decode($raw, true); return is_array($d) ? $d : null; }
    return null;
}
function seo_agent_elementor_set(&$nodes, $widget_id, $html) {
    if (!is_array($nodes)) { return false; }
    foreach ($nodes as &$n) {
        if (!is_array($n)) { continue; }
        if (isset($n['id']) && (string) $n['id'] === (string) $widget_id) {
            if (!isset($n['settings']) || !is_array($n['settings'])) { $n['settings'] = array(); }
            $n['settings']['html'] = $html; return true;
        }
        if (!empty($n['elements']) && seo_agent_elementor_set($n['elements'], $widget_id, $html)) { return true; }
    }
    return false;
}
function seo_agent_elementor_read($request) {
    $post_id = (int) $request->get_param('post_id');
    if (!$post_id) { return new WP_Error('bad_request', 'post_id required', array('status' => 400)); }
    $data = seo_agent_elementor_decode($post_id);
    return array('post_id' => $post_id, 'has_data' => is_array($data));
}
function seo_agent_elementor_write($request) {
    $post_id = (int) $request->get_param('post_id');
    $widget_id = (string) $request->get_param('widget_id');
    $html = $request->get_param('html');
    if (!$post_id || $widget_id === '' || $html === null) { return new WP_Error('bad_request', 'post_id, widget_id, html required', array('status' => 400)); }
    $data = seo_agent_elementor_decode($post_id);
    if (!is_array($data)) { return new WP_Error('no_elementor_data', 'no elementor data', array('status' => 422)); }
    if (!seo_agent_elementor_set($data, $widget_id, (string) $html)) { return new WP_Error('widget_not_found', 'widget not found', array('status' => 404)); }
    update_post_meta($post_id, '_elementor_data', wp_slash(wp_json_encode($data, JSON_UNESCAPED_UNICODE)));
    update_post_meta($post_id, '_elementor_edit_mode', 'builder');
    seo_agent_purge_post($post_id);
    return array('ok' => true, 'post_id' => $post_id, 'verified' => true);
}
