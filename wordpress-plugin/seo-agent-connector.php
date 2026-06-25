<?php
/**
 * Plugin Name: SEO Agent Connector
 * Description: Exposes Yoast SEO meta fields (title, description, robots) to the WordPress REST API so the SEO Agent System can read and update them using an application password. Adds nothing to the front end and changes no content.
 * Version: 1.0.0
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
