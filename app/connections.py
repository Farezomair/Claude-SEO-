"""Resolve a site's WordPress connection.

Order of preference:
1. The per-site connection saved in the Settings tab (stored in the database,
   password encrypted). This is the scalable path — adding a site never touches
   Railway.
2. The WORDPRESS_* environment variables, used ONLY when the site's URL host
   matches WORDPRESS_URL. This keeps the first site (Meridian, set up via env
   vars in Stage 3) working without re-entering its connection, while making
   sure those credentials are never applied to a different site.
"""
import os
from urllib.parse import urlparse

from .crypto import decrypt
from .database import SessionLocal
from .models import SiteConnection


def _host(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.lower().removeprefix("www.")


def get_connection(site_id: int, site_url: str = "", site_name: str = "") -> dict | None:
    db = SessionLocal()
    try:
        conn = db.query(SiteConnection).filter(SiteConnection.site_id == site_id).first()
        if conn and conn.wp_url and conn.wp_username and conn.wp_app_password_enc:
            return {
                "url": conn.wp_url,
                "username": conn.wp_username,
                "app_password": decrypt(conn.wp_app_password_enc),
                "site_name": site_name,
                "source": "settings",
            }
    finally:
        db.close()

    env_url = os.getenv("WORDPRESS_URL")
    env_user = os.getenv("WORDPRESS_USERNAME")
    env_pw = os.getenv("WORDPRESS_APP_PASSWORD")
    if env_url and env_user and env_pw and site_url and _host(site_url) == _host(env_url):
        return {
            "url": env_url,
            "username": env_user,
            "app_password": env_pw,
            "site_name": site_name,
            "source": "env",
        }
    return None
