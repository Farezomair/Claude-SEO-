# SEO Agent Connector (WordPress helper plugin)

A tiny one-file plugin that lets the SEO Agent System read and update **Yoast
SEO** meta fields (title, description, robots) through the WordPress REST API.

WordPress blocks these "protected" meta keys from REST writes by default. This
plugin opens just those four Yoast fields to authenticated requests (an admin
application password). It adds nothing to the front end and changes no content
— it only registers the fields for the REST API.

Install it once per site you want the agent to manage.

## Install — Option A: upload as a plugin (no file access needed)

1. Put `seo-agent-connector.php` into a folder named `seo-agent-connector`.
2. Zip that folder so you have `seo-agent-connector.zip`.
3. WP Admin → **Plugins → Add New → Upload Plugin** → choose the zip → **Install Now** → **Activate**.

## Install — Option B: drop-in must-use plugin (file/FTP access)

1. Upload `seo-agent-connector.php` to `wp-content/mu-plugins/`
   (create the `mu-plugins` folder if it doesn't exist).
2. That's it — must-use plugins activate automatically; there is no Activate step.

## Verify

Once installed and an application password exists, the SEO Agent System's
**Settings → Test connection** button will confirm it can reach the site.
