"""Read-only qualification of an already-installed local Ollama model."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from evallab.execution_contracts import (
    TERMINUS_LOCAL_ENDPOINT_ENV,
    TERMINUS_LOCAL_MODEL_SELECTOR,
)


@dataclass(frozen=True)
class OllamaBinding:
    selector: str
    model: str
    model_digest: str
    endpoint: str
    context_budget_tokens: int = 8192
    api_cost_usd: float = 0.0
    cost_basis: str = "local_inference_no_api_charge"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("local Ollama endpoints must not redirect")


def local_ollama_endpoint(environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    value = source.get(TERMINUS_LOCAL_ENDPOINT_ENV, "http://127.0.0.1:11434")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid local Ollama endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama must use a literal http://127.0.0.1:<port> endpoint")
    return f"http://127.0.0.1:{port}"


def resolve_ollama_binding(
    selector: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> OllamaBinding:
    """Check local inventory without starting a model, pulling weights, or spending."""
    if selector != TERMINUS_LOCAL_MODEL_SELECTOR:
        raise ValueError(f"local Terminus requires {TERMINUS_LOCAL_MODEL_SELECTOR!r}")
    endpoint = local_ollama_endpoint(environment)
    model_name = selector.split("/", 1)[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(f"{endpoint}/api/tags", timeout=2) as response:
            raw = response.read(1_048_577)
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError("local Ollama is unavailable; start the local server first") from exc
    if len(raw) > 1_048_576:
        raise ValueError("local Ollama inventory exceeds the supported response size")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("local Ollama returned an invalid inventory") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("local Ollama inventory has no models list")
    matches = [
        model
        for model in models
        if isinstance(model, dict) and model.get("name") == model_name
    ]
    if len(matches) != 1:
        raise ValueError(f"local Ollama model {model_name!r} is not installed; no download attempted")
    model = matches[0]
    details = model.get("details")
    digest = model.get("digest")
    size = model.get("size")
    if (
        model.get("remote_host")
        or model.get("remote_model")
        or not isinstance(details, dict)
        or details.get("format") != "gguf"
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("Terminus local execution requires an installed GGUF, not a cloud model")
    return OllamaBinding(
        selector=selector,
        model=model_name,
        model_digest=f"sha256:{digest}",
        endpoint=endpoint,
    )
