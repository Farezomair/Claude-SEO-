"""Thin wrapper around the Anthropic SDK: structured output with a refusal fallback."""
import json

import anthropic

from . import config

_client = None


def client():
    global _client
    if _client is None:
        config.require_anthropic_key()
        _client = anthropic.Anthropic(max_retries=3)
    return _client


def structured(system: str, user: str, schema, effort: str = "medium", max_tokens: int = 16000):
    """Return a validated pydantic instance of `schema`, or raise RuntimeError."""
    cfg = config.load()
    try:
        resp = client().messages.parse(
            model=cfg["model"],
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
            # Server-side fallback if the main model declines (beta).
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"rate limited: {e}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"connection error: {e}") from e
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("output cut off (max_tokens)")
    if resp.parsed_output is None:
        raise RuntimeError("no structured output returned")
    return resp.parsed_output


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
