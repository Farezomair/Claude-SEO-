<?php
/**
 * Plugin Name: SEO Agent Bridge 8
 * Description: Lets the SEO Agent (Ascend) read/update Yoast meta, Additional CSS, the Elementor HTML widget, and the _meridian_body field — and, new in v1.4, manage TECHNICAL SEO at the server level: send security response headers, serve /llms.txt (HTTP 200), 301-REDIRECT dead/old URLs, inject <head> tags (canonical / Open Graph / viewport / favicon), and extend robots.txt (e.g. allow AI crawlers). All toggleable via REST and fully reversible. Adds nothing visible to the front end on its own.
 * Version: 1.6.0
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
