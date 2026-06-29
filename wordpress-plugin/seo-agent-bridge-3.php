<?php
/**
 * Plugin Name: SEO Agent Bridge 3
 * Description: Lets the SEO Agent (Ascend) read and update Yoast SEO meta, the site's Additional CSS, and — new in v1.2 — the content of an Elementor "HTML" widget on a page, all via the REST API with an application password. PHP can edit the Elementor page data that the default REST/Abilities API will not expose, so the agent can fix page bodies headlessly. Adds nothing to the front end on its own.
 * Version: 1.2.0
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
    $post_types = array('post', 'page');
    foreach ($post_types as $post_type) {
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
 * 2) Additional CSS read/write (unchanged) + 3) Elementor widget edit (NEW)
 * ------------------------------------------------------------------------- */
add_action('rest_api_init', function () {

    // --- Additional CSS (the Customizer "Additional CSS"); fully reversible. ---
    register_rest_route('seo-agent/v1', '/custom-css', array(
        array(
            'methods'             => 'GET',
            'callback'            => function () {
                return array('css' => (string) wp_get_custom_css());
            },
            'permission_callback' => function () {
                return current_user_can('edit_theme_options');
            },
        ),
        array(
            'methods'             => 'POST',
            'callback'            => function ($request) {
                $css = (string) $request->get_param('css');
                wp_update_custom_css_post($css);
                return array('ok' => true, 'length' => strlen($css));
            },
            'permission_callback' => function () {
                return current_user_can('edit_theme_options');
            },
        ),
    ));

    // --- NEW: Elementor HTML-widget read/write -----------------------------
    // GET  /seo-agent/v1/elementor?post_id=123
    //      -> { post_id, has_data, widgets:[{id, type, html_len}], html_widget_count }
    // POST /seo-agent/v1/elementor  body: { post_id, widget_id, html }
    //      -> { ok, verified, live_len, method }
    register_rest_route('seo-agent/v1', '/elementor', array(
        array(
            'methods'             => 'GET',
            'callback'            => 'seo_agent_elementor_read',
            'permission_callback' => function () {
                return current_user_can('edit_pages');
            },
        ),
        array(
            'methods'             => 'POST',
            'callback'            => 'seo_agent_elementor_write',
            'permission_callback' => function () {
                return current_user_can('edit_pages');
            },
        ),
    ));
});

/**
 * Decode a post's _elementor_data (stored as a JSON string) into a PHP array.
 */
function seo_agent_elementor_decode($post_id) {
    $raw = get_post_meta($post_id, '_elementor_data', true);
    if (is_array($raw)) {
        return $raw;
    }
    if (is_string($raw) && $raw !== '') {
        $data = json_decode($raw, true);
        return is_array($data) ? $data : null;
    }
    return null;
}

/**
 * Walk the Elementor node tree, collecting every widget (id, type, html length).
 */
function seo_agent_elementor_collect($nodes, &$out) {
    if (!is_array($nodes)) {
        return;
    }
    foreach ($nodes as $node) {
        if (!is_array($node)) {
            continue;
        }
        if (isset($node['id'])) {
            $type = isset($node['widgetType']) ? $node['widgetType'] : (isset($node['elType']) ? $node['elType'] : '');
            $html = isset($node['settings']['html']) ? (string) $node['settings']['html'] : '';
            $out[] = array(
                'id'       => $node['id'],
                'type'     => $type,
                'html_len' => strlen($html),
            );
        }
        if (!empty($node['elements'])) {
            seo_agent_elementor_collect($node['elements'], $out);
        }
    }
}

/**
 * Recursively set settings.html on the node whose id === $widget_id.
 * Returns true if a node was updated.
 */
function seo_agent_elementor_set(&$nodes, $widget_id, $html) {
    if (!is_array($nodes)) {
        return false;
    }
    foreach ($nodes as &$node) {
        if (!is_array($node)) {
            continue;
        }
        if (isset($node['id']) && (string) $node['id'] === (string) $widget_id) {
            if (!isset($node['settings']) || !is_array($node['settings'])) {
                $node['settings'] = array();
            }
            $node['settings']['html'] = $html;
            return true;
        }
        if (!empty($node['elements'])) {
            if (seo_agent_elementor_set($node['elements'], $widget_id, $html)) {
                return true;
            }
        }
    }
    return false;
}

/**
 * Recursively read settings.html for a given widget id (for verification).
 */
function seo_agent_elementor_get_html($nodes, $widget_id) {
    if (!is_array($nodes)) {
        return null;
    }
    foreach ($nodes as $node) {
        if (!is_array($node)) {
            continue;
        }
        if (isset($node['id']) && (string) $node['id'] === (string) $widget_id) {
            return isset($node['settings']['html']) ? (string) $node['settings']['html'] : null;
        }
        if (!empty($node['elements'])) {
            $found = seo_agent_elementor_get_html($node['elements'], $widget_id);
            if ($found !== null) {
                return $found;
            }
        }
    }
    return null;
}

/**
 * GET handler: report the page's html widgets so the agent can target one.
 */
function seo_agent_elementor_read($request) {
    $post_id = (int) $request->get_param('post_id');
    if (!$post_id) {
        return new WP_Error('bad_request', 'post_id is required', array('status' => 400));
    }
    $data = seo_agent_elementor_decode($post_id);
    $widgets = array();
    if (is_array($data)) {
        seo_agent_elementor_collect($data, $widgets);
    }
    $html_widgets = array_values(array_filter($widgets, function ($w) {
        return $w['type'] === 'html';
    }));
    return array(
        'post_id'           => $post_id,
        'has_data'          => is_array($data),
        'widgets'           => $widgets,
        'html_widget_count' => count($html_widgets),
    );
}

/**
 * POST handler: set one html widget's content, refresh Elementor's CSS cache,
 * and verify the change by re-reading.
 */
function seo_agent_elementor_write($request) {
    $post_id   = (int) $request->get_param('post_id');
    $widget_id = (string) $request->get_param('widget_id');
    $html      = $request->get_param('html');

    if (!$post_id || $widget_id === '' || $html === null) {
        return new WP_Error('bad_request', 'post_id, widget_id and html are all required', array('status' => 400));
    }
    $html = (string) $html;

    $data = seo_agent_elementor_decode($post_id);
    if (!is_array($data)) {
        return new WP_Error('no_elementor_data', 'This page has no Elementor data to edit', array('status' => 422));
    }
    if (!seo_agent_elementor_set($data, $widget_id, $html)) {
        return new WP_Error('widget_not_found', 'No widget with that id on this page', array('status' => 404));
    }

    // Elementor stores _elementor_data as a slashed JSON string. update_metadata()
    // runs wp_unslash() on the value, so we wp_slash() our JSON to round-trip cleanly.
    $json = wp_json_encode($data, JSON_UNESCAPED_UNICODE);
    if ($json === false) {
        return new WP_Error('encode_failed', 'Could not encode the page data', array('status' => 500));
    }
    update_post_meta($post_id, '_elementor_data', wp_slash($json));
    update_post_meta($post_id, '_elementor_edit_mode', 'builder');

    // Force Elementor to regenerate this page's cached CSS so the change shows.
    delete_post_meta($post_id, '_elementor_css');
    if (class_exists('\\Elementor\\Plugin') && isset(\Elementor\Plugin::$instance->files_manager)) {
        try {
            \Elementor\Plugin::$instance->files_manager->clear_cache();
        } catch (\Throwable $e) {
            // best-effort
        }
    }
    clean_post_cache($post_id);

    // LiteSpeed (or other) full-page cache purge, best-effort.
    if (function_exists('do_action')) {
        do_action('litespeed_purge_post', $post_id);
        do_action('litespeed_purge_all');
    }

    // Verify by re-reading from the database.
    $check_html = seo_agent_elementor_get_html(seo_agent_elementor_decode($post_id), $widget_id);
    $verified = (is_string($check_html) && $check_html === $html);

    return array(
        'ok'        => true,
        'post_id'   => $post_id,
        'widget_id' => $widget_id,
        'verified'  => $verified,
        'live_len'  => is_string($check_html) ? strlen($check_html) : 0,
        'method'    => 'elementor-data-php',
    );
}
