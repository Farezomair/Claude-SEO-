"""WordPress Abilities API client — Ascend's headless execution layer.

Talks directly to the site's official Abilities API (`wp-abilities/v1`), which is
self-hosted on the WordPress site and authenticated with the same Application
Password we already use for the REST adapter. No third-party relay and no
interactive OAuth, so Ascend can discover and run abilities unattended — the
property WPVibe lacked (see docs/SESSION-HANDOFF.md).

REST contract (official WordPress Abilities API):
- Discover:  GET  /wp-json/wp-abilities/v1/abilities          -> [ability, ...]
- Inspect:   GET  /wp-json/wp-abilities/v1/abilities/{name}    -> ability
- Execute:   POST /wp-json/wp-abilities/v1/abilities/{name}/run  body {"input": {...}}

`{name}` is the full `namespace/ability-name`, so it contains a slash that maps to
two path segments. An ability object carries: name, label, description, category,
input_schema, output_schema, meta.
"""
import httpx

REQUEST_TIMEOUT = 30.0
USER_AGENT = "SEO-Agent/1.0"
API_BASE = "/wp-json/wp-abilities/v1"


def _get_params(value, prefix: str = "input") -> list[tuple[str, str]]:
    """Flatten an input dict into PHP bracket-notation query params.

    WordPress/PHP parses `input[post_type]=page&input[tags][]=1` into a nested
    object/array. A raw JSON string in `input=` is seen as a string and rejected
    ("input is not of type object"), so read-only (GET) abilities need this form.
    """
    params: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            params.extend(_get_params(v, f"{prefix}[{k}]"))
    elif isinstance(value, (list, tuple)):
        for v in value:
            params.extend(_get_params(v, f"{prefix}[]"))
    elif isinstance(value, bool):  # before int/str: bool is a subclass of int
        params.append((prefix, "true" if value else "false"))
    elif value is None:
        pass
    else:
        params.append((prefix, str(value)))
    return params


class AbilitiesError(Exception):
    pass


class AbilitiesUnavailable(AbilitiesError):
    """The Abilities API is not installed/active on this site (HTTP 404)."""


class AbilitiesClient:
    def __init__(self, base_url: str, username: str, app_password: str):
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/")
        self.auth = (username, app_password)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=REQUEST_TIMEOUT,
            auth=self.auth,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def available(self) -> bool:
        """True if the Abilities API answers on this site (cheap probe)."""
        try:
            with self._client() as c:
                r = c.get(f"{self.base}{API_BASE}/abilities", params={"per_page": 1})
            return r.status_code == 200
        except Exception:
            return False

    def list_abilities(self) -> list[dict]:
        """Return the full catalog of registered abilities (paginated-safe)."""
        items: list[dict] = []
        with self._client() as c:
            page = 1
            while True:
                r = c.get(
                    f"{self.base}{API_BASE}/abilities",
                    params={"per_page": 100, "page": page},
                )
                if r.status_code == 404:
                    raise AbilitiesUnavailable("Abilities API not found on this site.")
                if r.status_code in (401, 403):
                    raise AbilitiesError(
                        f"Not authorized to list abilities (HTTP {r.status_code}). "
                        "Check the Application Password / user role."
                    )
                if r.status_code != 200:
                    raise AbilitiesError(f"HTTP {r.status_code}: {r.text[:200]}")
                batch = r.json()
                if not isinstance(batch, list) or not batch:
                    break
                items.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return items

    def get_ability(self, name: str) -> dict:
        """Fetch one ability's full definition (incl. input/output schema)."""
        with self._client() as c:
            r = c.get(f"{self.base}{API_BASE}/abilities/{name}")
        if r.status_code == 404:
            raise AbilitiesUnavailable(f"Ability '{name}' not found.")
        if r.status_code != 200:
            raise AbilitiesError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json() or {}

    def read(self, name: str, input_data: dict | None = None) -> dict:
        """Run a READ-ONLY ability. These require GET with input as a query param."""
        return self.run(name, input_data, method="GET")

    def run(self, name: str, input_data: dict | None = None, method: str = "POST") -> dict:
        """Execute an ability.

        Write abilities take POST with input under the `input` key. Read-only
        abilities require GET with input as a URL-encoded `input` query param; if
        a POST is rejected with 405 (rest_ability_invalid_method) we transparently
        retry as GET. DESTRUCTIVE abilities (e.g. litespeed-cache-flush) require
        DELETE — pass method="DELETE" and we retry POST->DELETE on that same 405.
        """
        url = f"{self.base}{API_BASE}/abilities/{name}/run"
        input_data = input_data or {}
        m = method.upper()
        # An empty {} JSON-decodes to a PHP array server-side and fails the schema's
        # "type: object" check (e.g. litespeed-cache-flush takes no params). Send a
        # harmless sentinel key so the body stays a non-empty object; abilities whose
        # schema allows additional properties ignore it. Only added when truly empty.
        body_input = input_data if input_data else {"_": True}
        with self._client() as c:
            if m == "GET":
                r = c.get(url, params=_get_params(input_data))
            elif m in ("DELETE", "PUT", "PATCH"):
                r = c.request(m, url, json={"input": body_input})
            else:
                r = c.post(url, json={"input": body_input})
                if r.status_code == 405 and "invalid_method" in r.text:
                    # The API tells us which verb it wants; honour GET or DELETE
                    # (send the input body on DELETE too — the handler expects it).
                    r = (c.request("DELETE", url, json={"input": body_input}) if "DELETE" in r.text
                         else c.get(url, params=_get_params(input_data)))
        if r.status_code == 404:
            raise AbilitiesUnavailable(f"Ability '{name}' not found.")
        if r.status_code in (401, 403):
            raise AbilitiesError(f"Not authorized to run '{name}' (HTTP {r.status_code}).")
        if r.status_code not in (200, 201):
            raise AbilitiesError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
