"""Schema doer — inject Organization/LocalBusiness JSON-LD into the homepage.

Generates entity structured data from the homepage's real facts (via the
seo-schema rules in knowledge.SCHEMA_GUIDE) and appends it as a JSON-LD <script>
to the homepage Elementor html widget. Gated (it edits the homepage), invisible
on the page, verified after write, and revertible via the saved snapshot — reusing
the Elementor apply/verify path. Resolves no_entity_schema / no_localbusiness_schema.
"""
import json
import threading

from bs4 import BeautifulSoup

import json as _json

from .abilities import AbilitiesClient, AbilitiesError, AbilitiesUnavailable
from .brain import generate_schema_jsonld
from .crawler import LOCALBUSINESS_SUBTYPES
from .database import SessionLocal
from .elementor_agent import P, _find_html_widget, list_elementor_pages, read_body
from .models import Approval, Finding, JobRun, RunLog, Site, SiteChange

A_SETTINGS = f"{P}/wp-settings-get"
ENTITY_CATS = ("no_entity_schema", "no_localbusiness_schema")


def _iter_ld_objects(data):
    """Yield JSON-LD objects from a parsed block, flattening @graph and lists."""
    if isinstance(data, list):
        for d in data:
            yield from _iter_ld_objects(d)
    elif isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for d in data["@graph"]:
                yield from _iter_ld_objects(d)
        yield data


def _looks_like_entity(obj: dict) -> bool:
    t = obj.get("@type")
    types = [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]
    # Organization, anything with "Business" in the name, or a known LocalBusiness
    # subtype (GeneralContractor/Dentist/Plumber…) even when no contact fields are present.
    if any(x == "Organization" or "Business" in x or x.lower() in LOCALBUSINESS_SUBTYPES
           for x in types):
        return True
    # Fallback: a typed object carrying business-identity fields.
    if types and any(k in obj for k in
                     ("telephone", "address", "areaServed", "openingHours", "openingHoursSpecification")):
        return True
    return False


def _has_entity_schema(html: str) -> bool:
    """True if the page already carries Organization/LocalBusiness JSON-LD.

    Parses each ld+json block (instead of substring-matching), so it neither misses
    LocalBusiness SUBTYPES nor false-fires on the word 'Business' in visible copy."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = _json.loads(raw)
        except Exception:
            continue
        for obj in _iter_ld_objects(data):
            if isinstance(obj, dict) and _looks_like_entity(obj):
                return True
    return False


def _homepage_id(client: AbilitiesClient, conn: dict) -> int:
    """Best-effort: the front-page id from settings, else a page titled Home."""
    try:
        s = client.read(A_SETTINGS, {})
        pid = (s or {}).get("page_on_front")
        if pid:
            return int(pid)
    except Exception:
        pass
    try:
        pages = list_elementor_pages(conn)
        for p in pages:
            if (p.get("title") or "").strip().lower() in ("homepage", "home"):
                return int(p["id"])
        if pages:
            return int(pages[0]["id"])
    except Exception:
        pass
    return 0


def _close_entity_findings(db, site_id: int) -> None:
    for f in (db.query(Finding).filter(Finding.site_id == site_id,
                                        Finding.category.in_(ENTITY_CATS),
                                        Finding.status == "open").all()):
        f.status = "in-progress"
    db.commit()


def run_schema_inject(site_id: int, run_id: int, conn: dict) -> None:
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        site = db.get(Site, site_id)
        client = AbilitiesClient(conn["url"], conn["username"], conn["app_password"])
        if not client.available():
            run.status = "failed"
            run.summary = "The site's Abilities API is not reachable."
            db.commit()
            return

        pid = _homepage_id(client, conn)
        if not pid:
            run.status = "failed"
            run.summary = "Could not identify the homepage to add schema to."
            db.commit()
            return
        # Read the LIVE render source: the _meridian_body field the theme prints.
        old_html = read_body(client, pid)
        if old_html is None:
            run.status = "failed"
            run.summary = "Couldn't read the homepage body — is SEO Agent Bridge (v4+) active?"
            db.commit()
            return
        if not old_html:
            run.status = "failed"
            run.summary = "The homepage body is empty — nothing to attach schema to."
            db.commit()
            return

        # Already has entity schema? Don't double-inject (parse, don't substring-match).
        if _has_entity_schema(old_html):
            _close_entity_findings(db, site_id)
            run.status = "completed"
            run.summary = "Homepage already has entity schema — nothing to add."
            db.commit()
            return

        text = BeautifulSoup(old_html, "html.parser").get_text(" ", strip=True)
        try:
            jsonld = generate_schema_jsonld(site.name, site.url, text)
        except Exception as exc:
            run.status = "failed"
            run.summary = f"Schema generation failed: {exc.__class__.__name__}: {exc}"
            db.commit()
            return

        jstr = json.dumps(jsonld, ensure_ascii=False, indent=2)
        script = f'\n<script type="application/ld+json">\n{jstr}\n</script>\n'
        new_html = old_html + script
        schema_type = jsonld.get("@type", "LocalBusiness")
        if isinstance(schema_type, list):
            schema_type = schema_type[0] if schema_type else "LocalBusiness"

        change = SiteChange(
            site_id=site_id, kind="schema_inject",
            request=f"Add {schema_type} schema to homepage",
            css=new_html, old_css=old_html, status="proposed",
            target_page_id=pid, target_widget_id="",
        )
        db.add(change)
        db.commit()
        db.refresh(change)
        db.add(Approval(
            site_id=site_id, kind="schema_inject",
            title=f"Add {schema_type} schema to homepage",
            summary="Adds Organization/LocalBusiness structured data so Google and AI "
                    "assistants understand your business. Invisible on the page; one-click revert.",
            payload=json.dumps({"change_id": change.id, "page_id": pid, "jsonld": jstr}),
            status="pending",
        ))
        _close_entity_findings(db, site_id)  # in-progress until approved
        run.status = "completed"
        run.summary = f"Proposed {schema_type} schema for the homepage — waiting for approval."
        db.add(RunLog(site_id=site_id, message=run.summary))
        db.commit()
    except Exception as exc:
        run = db.get(JobRun, run_id)
        if run:
            run.status = "failed"
            run.summary = f"Run failed: {exc.__class__.__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def start_schema_inject_async(site_id: int, run_id: int, conn: dict) -> None:
    threading.Thread(target=run_schema_inject, args=(site_id, run_id, conn), daemon=True).start()
