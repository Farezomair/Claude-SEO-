"""Google Search Console client + the findings it produces (Phase B/Google).

Read-only: pulls real search performance (queries, positions, CTR) and turns it
into routed Findings the SEO Auditor adds to an audit — the data the free crawl
cannot see. Used only when a Google connection exists and the site matches a
Search Console property the account owns.
"""
from datetime import date, timedelta
from urllib.parse import quote, urlparse

import httpx

from .google_oauth import get_access_token

SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"


def _host(url: str) -> str:
    netloc = urlparse(url if "://" in url else "https://" + url).netloc
    return netloc.lower().removeprefix("www.")


def list_properties(access_token: str) -> list[str]:
    try:
        r = httpx.get(SITES_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=20.0)
        if r.status_code != 200:
            return []
        return [s.get("siteUrl", "") for s in r.json().get("siteEntry", [])]
    except Exception:
        return []


def match_property(access_token: str, site_url: str) -> str | None:
    host = _host(site_url)
    for prop in list_properties(access_token):
        ph = (prop.replace("sc-domain:", "").replace("https://", "").replace("http://", "")
              .strip("/").lower().removeprefix("www."))
        if ph == host:
            return prop
    return None


def search_analytics(access_token: str, prop: str, days: int = 90,
                     dimensions=("query",), row_limit: int = 250) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": list(dimensions), "rowLimit": row_limit}
    try:
        r = httpx.post(
            f"{SITES_URL}/{quote(prop, safe='')}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {access_token}"}, json=body, timeout=30.0,
        )
        return r.json().get("rows", []) if r.status_code == 200 else []
    except Exception:
        return []


def _lookback() -> int:
    """Enhance-bar tunable: Search Console lookback window (days)."""
    from .capabilities import cap_setting
    try:
        return int(cap_setting("ranking", "gsc_lookback_days", 90))
    except Exception:
        return 90


def gsc_findings(site_url: str) -> list[dict]:
    """Return audit-issue dicts from Search Console, or [] if not connected/matched."""
    token = get_access_token()
    if not token:
        return []
    prop = match_property(token, site_url)
    if not prop:
        return []

    issues: list[dict] = []
    # Striking-distance queries: ranking pos 5-20 with real impressions = page-1 opportunities.
    for row in search_analytics(token, prop, days=_lookback(), dimensions=("query",)):
        keys = row.get("keys") or [""]
        q, pos, imp = keys[0], row.get("position", 0), row.get("impressions", 0)
        if 5 <= pos <= 20 and imp >= 20:
            issues.append({
                "category": "striking_distance", "severity": "medium", "url": site_url,
                "detail": f"Query \"{q}\" ranks #{pos:.0f} with {imp} impressions — push to page 1",
                "finding_type": "opportunity", "detection_source": "search console",
            })
    # Low-CTR pages: high impressions, weak click-through = title/meta opportunity.
    for row in search_analytics(token, prop, days=_lookback(), dimensions=("page",)):
        keys = row.get("keys") or [""]
        page, imp, ctr, pos = keys[0], row.get("impressions", 0), row.get("ctr", 0), row.get("position", 0)
        if imp >= 100 and pos <= 10 and ctr < 0.02:
            issues.append({
                "category": "low_ctr", "severity": "medium", "url": page,
                "detail": f"Page gets {imp} impressions at #{pos:.0f} but only {ctr*100:.1f}% CTR — improve title/description",
                "finding_type": "opportunity", "detection_source": "search console",
            })
    # Most valuable first, capped.
    issues.sort(key=lambda i: 0 if i["category"] == "striking_distance" else 1)
    return issues[:20]


def queries_by_page(site_url: str, days: int = 90, top_n: int = 6) -> dict:
    """Real Search Console demand per page: {path: [{query, clicks, impressions,
    position}, ...]} (top queries by impressions). {} when GSC isn't connected."""
    token = get_access_token()
    if not token:
        return {}
    prop = match_property(token, site_url)
    if not prop:
        return {}
    out: dict = {}
    try:
        for row in search_analytics(token, prop, days=days, dimensions=("page", "query")):
            keys = row.get("keys") or []
            if len(keys) < 2:
                continue
            from urllib.parse import urlparse
            path = (urlparse(keys[0]).path or "/").rstrip("/") or "/"
            out.setdefault(path, []).append({
                "query": keys[1],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "position": round(float(row.get("position", 0)), 1),
            })
    except Exception:
        return {}
    for path in out:
        out[path] = sorted(out[path], key=lambda r: -r["impressions"])[:top_n]
    return out
