<?php
/**
 * Plugin Name: SEO Agent Connector
 * Description: Exposes Yoast SEO meta fields and the site's Additional CSS to the WordPress REST API so the SEO Agent System can read and update them using an application password. Adds nothing to the front end and changes no content.
 * Version: 1.1.0
 * Author: SEO Agent System
 */

if (!defined('ABSPATH')) {
    exit; // Run only inside WordPress.
}

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
                'show_in_rest' => true,
                'single'       => true,
                'type'         => 'string',
                // Only users who can edit posts (i.e. an authenticated admin
                // application password) may read or write these via REST.
                'auth_callback' => function () {
                    return current_user_can('edit_posts');
                },
            ));
        }
    }
});

// Expose the site's Additional CSS (the WordPress Customizer "Additional CSS").
// This is fully reversible and cannot take the site down, so it's the safe way
// for the Website agent to make visual changes.
add_action('rest_api_init', function () {
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
});
