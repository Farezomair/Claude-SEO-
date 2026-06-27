"""Audit scoring — turns a flat list of findings into a graded, prioritized audit.

The old auditor only severity-tagged issues. This converts findings into:
- a per-category score (0-100) and letter grade,
- a weighted overall health score (0-100) + grade,
- a prioritized fix plan ranked by impact (severity) and effort (how the fix is
  applied: auto-safe < needs-approval < needs-human).

Scores start at 100 per category and lose points per finding by severity. Only
categories we actually evaluate are weighted (so an un-run category never inflates
the score). Weights mirror the SEO-audit skill's category weighting.
"""
from .routing import classify

SEVERITY_PENALTY = {"blocker": 40, "critical": 28, "high": 16, "medium": 8, "low": 3}
SEVERITY_IMPACT = {"blocker": 5, "critical": 4, "high": 3, "medium": 2, "low": 1}
EFFORT = {"auto-safe": 1, "needs-approval": 2, "needs-human": 3}

# Finding group (from routing) -> scoring category.
CATEGORY_OF_GROUP = {
    "site-integrity": "technical", "indexation": "technical", "security": "technical",
    "structure": "technical", "mobile": "technical", "required-pages": "technical",
    "uncategorized": "technical",
    "on-page": "onpage", "meta": "onpage", "ranking": "onpage",
    "content-depth": "content", "content": "content",
    "schema": "schema",
    "ai-geo": "geo",
    "local": "local",
    "images": "images",
    "performance": "performance",
}

# (key, label, weight). Weights sum to 1.0 across everything we can measure.
CATEGORIES = [
    ("technical", "Technical", 0.24),
    ("onpage", "On-page", 0.20),
    ("content", "Content & E-E-A-T", 0.20),
    ("schema", "Schema", 0.08),
    ("geo", "AI / GEO", 0.08),
    ("local", "Local", 0.05),
    ("images", "Images", 0.05),
    ("performance", "Performance", 0.10),
]
_LABEL = {k: label for k, label, _ in CATEGORIES}

# Categories the current battery can actually evaluate (performance arrives with
# the PageSpeed analyzer; until then it is excluded so it can't inflate the score).
MEASURED = {"technical", "onpage", "content", "schema", "geo", "local", "images"}


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def compute(issues: list[dict], measured: set | None = None) -> dict:
    """Score a list of issue dicts (category, severity, url, detail)."""
    measured = measured if measured is not None else MEASURED
    pen = {k: 0 for k, _, _ in CATEGORIES}
    count = {k: 0 for k, _, _ in CATEGORIES}
    enriched = []
    for iss in issues:
        cls = classify(iss["category"])
        cat = CATEGORY_OF_GROUP.get(cls["group"], "technical")
        sev = iss.get("severity", "low")
        pen[cat] = min(100, pen[cat] + SEVERITY_PENALTY.get(sev, 3))
        count[cat] += 1
        enriched.append({
            "detail": iss.get("detail", ""), "severity": sev, "url": iss.get("url", ""),
            "category": iss["category"], "cat": cat, "route": cls["route"],
            "impact": SEVERITY_IMPACT.get(sev, 1), "effort": EFFORT.get(cls["action_class"], 2),
        })

    cats = []
    for key, label, weight in CATEGORIES:
        if key not in measured:
            continue
        score = max(0, 100 - pen[key])
        cats.append({"key": key, "label": label, "score": score,
                     "grade": _grade(score), "weight": weight, "count": count[key]})
    total_w = sum(c["weight"] for c in cats) or 1.0
    overall = round(sum(c["score"] * c["weight"] for c in cats) / total_w)

    # Prioritized fix plan: biggest impact first, least effort to break ties.
    ranked = sorted(enriched, key=lambda x: (-x["impact"], x["effort"]))
    seen, roadmap = set(), []
    for r in ranked:
        key = (r["cat"], r["detail"][:60])
        if key in seen:
            continue
        seen.add(key)
        roadmap.append({"title": r["detail"], "severity": r["severity"],
                        "category_label": _LABEL.get(r["cat"], r["cat"]),
                        "route": r["route"], "url": r["url"]})
        if len(roadmap) >= 8:
            break

    return {"overall": overall, "grade": _grade(overall), "categories": cats, "roadmap": roadmap}
