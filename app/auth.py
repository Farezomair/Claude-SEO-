"""Single-owner login.

There is no signup and no user management — by design. One owner, one set of
credentials, both supplied as environment variables (set as secrets in Railway,
never committed). Login state is kept in a signed session cookie.
"""
import hmac
import os

from starlette.requests import Request

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


def check_credentials(username: str, password: str) -> bool:
    """Constant-time check of submitted credentials against the env vars.

    Returns False if APP_PASSWORD is unset, so a misconfigured deploy fails
    closed (nobody can log in) rather than open.
    """
    if not APP_PASSWORD:
        return False
    user_ok = hmac.compare_digest(username.encode(), APP_USERNAME.encode())
    pass_ok = hmac.compare_digest(password.encode(), APP_PASSWORD.encode())
    return user_ok and pass_ok


def current_user(request: Request):
    """Return the logged-in username, or None."""
    return request.session.get("user")
