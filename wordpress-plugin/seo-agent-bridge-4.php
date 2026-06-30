<?php
/**
 * Plugin Name: SEO Agent Bridge 4
 * Description: Lets the SEO Agent (Ascend) read and update Yoast meta, Additional CSS, Elementor HTML-widget content, and — new in v1.3 — the custom "_meridian_body" field that the child theme's meridian-full-page.php template actually prints on the front end. PHP can edit the protected post-meta the default REST/Abilities API won't expose, so the agent can fix the LIVE page body headlessly. Adds nothing to the front end on its own.
 * Version: 1.3.0
 * Author: SEO Agent System
 */

if (!defined('ABSPATH')) {
    exit; // Run only inside WordPress.
}

/* ---------------------------------------------------------------------------
 * 1) Yoast SEO meta -> REST (unchanged from earlier bridge versions)
 * ------------------------------------------------------------------------- */
add_action('init', function () {
    $meta_keys = array(
        '_yoast_wpseo_title',
        '_yoast_wpseo_metadesc',
        '_yoast_wpseo_meta-robots-noindex',
        '_yoast_wpseo_meta-robots-nofollow',
    );
    foreach (array('post', 'page') as $post_type) {
        foreach ($meta_keys as $key) {
            register_post_meta($post_type, $key, array(
                'show_in_rest'  => true,
                'single'        => true,
                'type'          => 'string',
                'auth_callback' => function () {
                    return current_user_can('edit_posts');
                },
            ));
        }
    }
});

/* ---------------------------------------------------------------------------
 * Shared: purge caches for a post (LiteSpeed plugin + Hostinger via save_post).
 * ------------------------------------------------------------------------- */
function seo_agent_purge_post($post_id) {
    // Fire the normal save flow so Hostinger/LiteSpeed auto-purge this page.
    wp_update_post(array('ID' => (int) $post_id));
    clean_post_cache($post_id);
    // Explicit LiteSpeed purges (no-ops if the plugin isn't the cache layer).
    do_action('litespeed_purge_post', $post_id);
    $link = get_permalink($post_id);
    if ($link) {
        do_action('litespeed_purge_url', $link);
    }
    do_action('litespeed_purge_all');
    // Elementor CSS regen, in case anything still reads the Elementor copy.
    delete_post_meta($post_id, '_elementor_css');
}

/* ---------------------------------------------------------------------------
 * 2) Additional CSS + 3) Elementor widget + 4) _meridian_body (NEW)
 * ------------------------------------------------------------------------- */
add_action('rest_api_init', function () {

    // --- Additional CSS (Customizer "Additional CSS"); fully reversible. ---
    register_rest_route('seo-agent/v1', '/custom-css', array(
        array(
            'methods'             => 'GET',
            'callback'            => function () { return array('css' => (string) wp_get_custom_css()); },
            'permission_callback' => function () { return current_user_can('edit_theme_options'); },
        ),
        array(
            'methods'             => 'POST',
            'callback'            => function ($request) {
                $css = (string) $request->get_param('css');
                wp_update_custom_css_post($css);
                return array('ok' => true, 'length' => strlen($css));
            },
            'permission_callback' => function () { return current_user_can('edit_theme_options'); },
        ),
    ));

    // --- Elementor HTML-widget read/write (kept from Bridge 3) -------------
    register_rest_route('seo-agent/v1', '/elementor', array(
        array('methods' => 'GET',  'callback' => 'seo_agent_elementor_read',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_elementor_write',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
    ));

    // --- NEW: the custom _meridian_body field the theme actually renders ----
    // GET  /seo-agent/v1/body?post_id=123  -> { post_id, has_body, body_len, img_count, meridian_keys }
    // POST /seo-agent/v1/body  { post_id, html }  -> { ok, verified, body_len }
    register_rest_route('seo-agent/v1', '/body', array(
        array('methods' => 'GET',  'callback' => 'seo_agent_body_read',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
        array('methods' => 'POST', 'callback' => 'seo_agent_body_write',
              'permission_callback' => function () { return current_user_can('edit_pages'); }),
    ));
});

/* ----- _meridian_body handlers ------------------------------------------- */
function seo_agent_body_read($request) {
    $post_id = (int) $request->get_param('post_id');
    if (!$post_id) {
        return new WP_Error('bad_request', 'post_id is required', array('status' => 400));
    }
    $body = (string) get_post_meta($post_id, '_meridian_body', true);
    // List any _meridian_* keys present (helps confirm the render source).
    $all = get_post_meta($post_id);
    $mkeys = array();
    if (is_array($all)) {
        foreach ($all as $k => $v) {
            if (strpos($k, 'meridian') !== false) {
                $val = is_array($v) ? (isset($v[0]) ? $v[0] : '') : $v;
                $mkeys[$k] = strlen((string) $val);
            }
        }
    }
    $out = array(
        'post_id'       => $post_id,
        'has_body'      => $body !== '',
        'body_len'      => strlen($body),
        'img_count'     => preg_match_all('/<img\b/i', $body),
        'meridian_keys' => $mkeys,
    );
    // Return the full HTML only when asked (the agent needs it to edit; browser
    // diagnostics omit it to stay light).
    if ($request->get_param('include_html')) {
        $out['html'] = $body;
    }
    return $out;
}

function seo_agent_body_write($request) {
    $post_id = (int) $request->get_param('post_id');
    $html    = $request->get_param('html');
    if (!$post_id || $html === null) {
        return new WP_Error('bad_request', 'post_id and html are required', array('status' => 400));
    }
    $html = (string) $html;
    update_post_meta($post_id, '_meridian_body', wp_slash($html));
    seo_agent_purge_post($post_id);
    $live = (string) get_post_meta($post_id, '_meridian_body', true);
    return array(
        'ok'       => true,
        'post_id'  => $post_id,
        'verified' => ($live === $html),
        'body_len' => strlen($live),
    );
}

/* ----- Elementor handlers (kept from Bridge 3) --------------------------- */
function seo_agent_elementor_decode($post_id) {
    $raw = get_post_meta($post_id, '_elementor_data', true);
    if (is_array($raw)) { return $raw; }
    if (is_string($raw) && $raw !== '') {
        $data = json_decode($raw, true);
        return is_array($data) ? $data : null;
    }
    return null;
}

function seo_agent_elementor_collect($nodes, &$out) {
    if (!is_array($nodes)) { return; }
    foreach ($nodes as $node) {
        if (!is_array($node)) { continue; }
        if (isset($node['id'])) {
            $type = isset($node['widgetType']) ? $node['widgetType'] : (isset($node['elType']) ? $node['elType'] : '');
            $html = isset($node['settings']['html']) ? (string) $node['settings']['html'] : '';
            $out[] = array('id' => $node['id'], 'type' => $type, 'html_len' => strlen($html));
        }
        if (!empty($node['elements'])) { seo_agent_elementor_collect($node['elements'], $out); }
    }
}

function seo_agent_elementor_set(&$nodes, $widget_id, $html) {
    if (!is_array($nodes)) { return false; }
    foreach ($nodes as &$node) {
        if (!is_array($node)) { continue; }
        if (isset($node['id']) && (string) $node['id'] === (string) $widget_id) {
            if (!isset($node['settings']) || !is_array($node['settings'])) { $node['settings'] = array(); }
            $node['settings']['html'] = $html;
            return true;
        }
        if (!empty($node['elements'])) {
            if (seo_agent_elementor_set($node['elements'], $widget_id, $html)) { return true; }
        }
    }
    return false;
}

function seo_agent_elementor_get_html($nodes, $widget_id) {
    if (!is_array($nodes)) { return null; }
    foreach ($nodes as $node) {
        if (!is_array($node)) { continue; }
        if (isset($node['id']) && (string) $node['id'] === (string) $widget_id) {
            return isset($node['settings']['html']) ? (string) $node['settings']['html'] : null;
        }
        if (!empty($node['elements'])) {
            $found = seo_agent_elementor_get_html($node['elements'], $widget_id);
            if ($found !== null) { return $found; }
        }
    }
    return null;
}

function seo_agent_elementor_read($request) {
    $post_id = (int) $request->get_param('post_id');
    if (!$post_id) { return new WP_Error('bad_request', 'post_id is required', array('status' => 400)); }
    $data = seo_agent_elementor_decode($post_id);
    $widgets = array();
    if (is_array($data)) { seo_agent_elementor_collect($data, $widgets); }
    $html_widgets = array_values(array_filter($widgets, function ($w) { return $w['type'] === 'html'; }));
    return array('post_id' => $post_id, 'has_data' => is_array($data),
                 'widgets' => $widgets, 'html_widget_count' => count($html_widgets));
}

function seo_agent_elementor_write($request) {
    $post_id   = (int) $request->get_param('post_id');
    $widget_id = (string) $request->get_param('widget_id');
    $html      = $request->get_param('html');
    if (!$post_id || $widget_id === '' || $html === null) {
        return new WP_Error('bad_request', 'post_id, widget_id and html are all required', array('status' => 400));
    }
    $html = (string) $html;
    $data = seo_agent_elementor_decode($post_id);
    if (!is_array($data)) { return new WP_Error('no_elementor_data', 'This page has no Elementor data', array('status' => 422)); }
    if (!seo_agent_elementor_set($data, $widget_id, $html)) {
        return new WP_Error('widget_not_found', 'No widget with that id on this page', array('status' => 404));
    }
    $json = wp_json_encode($data, JSON_UNESCAPED_UNICODE);
    if ($json === false) { return new WP_Error('encode_failed', 'Could not encode the page data', array('status' => 500)); }
    update_post_meta($post_id, '_elementor_data', wp_slash($json));
    update_post_meta($post_id, '_elementor_edit_mode', 'builder');
    seo_agent_purge_post($post_id);
    $check_html = seo_agent_elementor_get_html(seo_agent_elementor_decode($post_id), $widget_id);
    return array('ok' => true, 'post_id' => $post_id, 'widget_id' => $widget_id,
                 'verified' => (is_string($check_html) && $check_html === $html),
                 'live_len' => is_string($check_html) ? strlen($check_html) : 0,
                 'method' => 'elementor-data-php');
}
