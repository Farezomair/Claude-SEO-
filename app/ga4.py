"""Google Analytics 4 (Data API) — organic traffic per landing page.

Rides the same Google OAuth connection as Search Console; needs the
`analytics.readonly` scope (granted when the owner reconnects Google after the
scope was added). Best-effort: every call returns a safe empty value on any
failure, so an enabled-but-unauthorized GA4 toggle can never break a doer.
"""
import httpx

from .google_oauth import get_access_token

ADMIN_URL = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
DATA_URL = "https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport"
TIMEOUT = 25.0


def _property_id(token: str) -> str:
    """First GA4 property on the account ('' if none / no scope)."""
    try:
        r = httpx.get(ADMIN_URL, headers={"Authorization": f"Bearer {token}"},
                      params={"pageSize": 50}, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        for acct in (r.json().get("accountSummaries") or []):
            for prop in (acct.get("propertySummaries") or []):
                pid = (prop.get("property") or "").split("/")[-1]
                if pid:
                    return pid
    except Exception:
        pass
    return ""


def organic_sessions_by_page(days: int = 28) -> dict:
    """{landing_page_path: organic sessions over the window}. {} when GA4 isn't
    reachable (no connection, missing scope, or no property)."""
    token = get_access_token()
    if not token:
        return {}
    pid = _property_id(token)
    if not pid:
        return {}
    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "landingPagePlusQueryString"}],
        "metrics": [{"name": "sessions"}],
        "dimensionFilter": {"filter": {
            "fieldName": "sessionDefaultChannelGroup",
            "stringFilter": {"value": "Organic Search"},
        }},
        "limit": 250,
    }
    try:
        r = httpx.post(DATA_URL.format(pid=pid), json=body,
                       headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        out = {}
        for row in (r.json().get("rows") or []):
            path = (row["dimensionValues"][0]["value"] or "/").split("?")[0].rstrip("/") or "/"
            out[path] = out.get(path, 0) + int(row["metricValues"][0]["value"] or 0)
        return out
    except Exception:
        return {}
