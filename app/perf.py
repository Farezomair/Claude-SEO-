"""Performance / Core Web Vitals via the free Google PageSpeed Insights API.

PSI returns real-user FIELD data (CrUX) when available, else LAB (Lighthouse)
estimates. No OAuth/service-account needed — works keyless (rate-limited) or with
an optional PAGESPEED_API_KEY. Best-effort: returns ([], False) on any failure so
the audit never breaks. Second return value says whether we got usable data (so
the Performance category is only scored when actually measured).
"""
import os

import httpx

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT = 60.0

# metric key -> (label, formatter)
_FIELD = {
    "LARGEST_CONTENTFUL_PAINT_MS": ("LCP", lambda v: f"{v/1000:.1f}s"),
    "INTERACTION_TO_NEXT_PAINT": ("INP", lambda v: f"{int(v)}ms"),
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("CLS", lambda v: f"{v/100:.2f}"),
}


def analyze_performance(start_url: str) -> tuple[list[dict], bool]:
    params = {"url": start_url, "strategy": "mobile", "category": "performance"}
    key = os.getenv("PAGESPEED_API_KEY")
    if key:
        params["key"] = key
    try:
        r = httpx.get(PSI_URL, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return [], False
        data = r.json()
    except Exception:
        return [], False

    issues: list[dict] = []
    metrics = (data.get("loadingExperience") or {}).get("metrics") or {}
    for key_name, (label, fmt) in _FIELD.items():
        m = metrics.get(key_name)
        if not m:
            continue
        cat = m.get("category")
        pct = m.get("percentile")
        if cat == "POOR":
            sev = "high"
        elif cat == "NEEDS_IMPROVEMENT":
            sev = "medium"
        else:
            continue  # GOOD — no finding
        val = fmt(pct) if pct is not None else "?"
        issues.append({
            "category": "cwv_poor", "severity": sev, "url": start_url,
            "detail": f"{label} is {val} ({cat.replace('_', ' ').lower()}) on real mobile users",
            "detection_source": "crux",
        })

    if metrics:
        return issues, True  # had field data

    # No field data (low-traffic site): fall back to the lab performance score.
    lh = data.get("lighthouseResult") or {}
    score = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    if score is None:
        return [], False
    if score < 0.5:
        issues.append({
            "category": "cwv_poor", "severity": "medium", "url": start_url,
            "detail": f"Lab performance score {int(score*100)}/100 (estimate — not enough real-user data yet)",
            "detection_source": "lighthouse",
        })
    return issues, True
