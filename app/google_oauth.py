"""Google OAuth (Search Console, read-only) — the one-click connect (Phase B/Google).

One-time setup: an OAuth client (ID + secret) created once in Google Cloud,
supplied via env vars. After that the owner clicks "Connect Google → Allow" and
we store an encrypted refresh token that reads every Search Console property the
account owns. No per-site service accounts, no key files.
"""
import os
from urllib.parse import urlencode

import httpx

from .crypto import decrypt, encrypt
from .database import SessionLocal
from .models import GoogleAuth, utcnow

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
SCOPE = "openid email https://www.googleapis.com/auth/webmasters.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def auth_url(redirect_uri: str) -> str:
    params = {
        "client_id": CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> tuple[str, str]:
    """Return (refresh_token, email). Raises on failure."""
    r = httpx.post(TOKEN_URL, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=20.0)
    r.raise_for_status()
    tok = r.json()
    refresh = tok.get("refresh_token", "")
    access = tok.get("access_token", "")
    email = ""
    try:
        u = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"}, timeout=15.0)
        if u.status_code == 200:
            email = u.json().get("email", "")
    except Exception:
        pass
    return refresh, email


def save_connection(refresh_token: str, email: str) -> None:
    db = SessionLocal()
    try:
        row = db.query(GoogleAuth).first()
        if not row:
            row = GoogleAuth()
            db.add(row)
        if refresh_token:
            row.refresh_token_enc = encrypt(refresh_token)
        row.email = email
        row.updated_at = utcnow()
        db.commit()
    finally:
        db.close()


def connection() -> dict | None:
    db = SessionLocal()
    try:
        row = db.query(GoogleAuth).first()
        if row and row.refresh_token_enc:
            return {"email": row.email}
        return None
    finally:
        db.close()


def disconnect() -> None:
    db = SessionLocal()
    try:
        row = db.query(GoogleAuth).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def get_access_token() -> str | None:
    db = SessionLocal()
    try:
        row = db.query(GoogleAuth).first()
        if not row or not row.refresh_token_enc:
            return None
        refresh = decrypt(row.refresh_token_enc)
    finally:
        db.close()
    try:
        r = httpx.post(TOKEN_URL, data={
            "refresh_token": refresh, "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
        }, timeout=20.0)
        return r.json().get("access_token") if r.status_code == 200 else None
    except Exception:
        return None
