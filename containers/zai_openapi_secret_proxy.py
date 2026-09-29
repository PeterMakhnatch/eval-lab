"""Least-privilege metered reverse proxy for pinned OpenAI-compatible providers.

Only this sidecar mounts the Z.ai Open Platform provider key. Untrusted task/agent
processes see an internal endpoint and a per-trial capability, never the credential.
Enforces the ``zai/`` model prefix, strips inbound credential headers, injects provider
auth only in the proxy process, and exposes no secret in config/log/error surfaces.
Connects strictly to the Open Platform Standard API (https://api.z.ai/api/paas/v4),
completely decoupled from the Coding Plan endpoint.

Hardening features:
- Worker bounding before thread creation: rejects excess connections with nonblocking 503.
- Inbound request deadline: wall-clock timer covers headers+body acquisition.
- Separate upstream timeout (120s) allowing long model generation without client socket cancellation.
- Graceful SIGTERM: stop accepting, let in-flight provider calls settle with their real usage,
  then fail closed on calls still in flight after the drain deadline.
- Pre-body capability authentication: rejects unauthenticated requests before reading body.
- Strict upstream response reading: requires bytes_read == declared Content-Length; EOF-short payloads return 502.
- Size-bounded upstream response reading (limit+1) with sanitized 502 classification.
- Multi-encoding secret redaction (raw, JSON, Base64, URL-encoded, Unicode, UTF-16, Bearer).
- Trial budget accounting with durable usage reports and SSE response handling.
- Provider-returned model identity verification.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_zai_openapi_api_key")
ALLOWED_PATH = "/api/paas/v4/chat/completions"


def _allowed_inbound_paths() -> frozenset[str]:
    """Chat-completions paths this proxy accepts for the active provider."""
    return frozenset(
        {"/chat/completions", "/v1/chat/completions", str(_profile()["upstream_path"])}
    )

HEALTHZ_PATH = "/healthz"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
# Model generation may legitimately outlast a short HTTP request deadline.
# The trial watchdog and pre-call token/cost reservations remain authoritative.
UPSTREAM_TIMEOUT_SECONDS = 600.0
# On SIGTERM, how long in-flight provider calls may run on to settle. A call
# whose client has gone (Harbor cancels the agent at its timeout) still
# completes upstream and is reconciled with its real usage; a call still in
# flight after this deadline is marked unresolved (``in_flight_at_shutdown``).
# The host supervisor's stop timeout must exceed it (runner.py
# ``_TERMINUS_PROXY_STOP_TIMEOUT_SECONDS``).
SHUTDOWN_DRAIN_SECONDS = 120.0
MAX_CONCURRENT_WORKERS = 32

ALLOWED_HTTP_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "evallab-smoke-upstream", "host.docker.internal"}
)

# ---------------------------------------------------------------------------
# OpenRouter route pins (HAR-104). Mirrors OPENROUTER_* in
# src/evallab/execution_contracts.py — this standalone container script cannot
# import that module. The treatment key reads these literals back from this
# file at the run's commit, so they must stay plain assignments.

# The pinned Xiaomi endpoint (tag xiaomi/fp8) serves and prices every call:
# pinning the serving provider keeps upstream behavior and the per-token price
# exact, and refuses OpenRouter's fallback pool.
OPENROUTER_PROVIDER_PIN = {"order": ["xiaomi"], "allow_fallbacks": False}
# MiMo thinking on, matching the self-hosted MiMo treatment.
OPENROUTER_REASONING_PIN = {"enabled": True}
# OpenRouter list price for xiaomi/mimo-v2.6-flash (verified 2026-09-29):
# $0.14 per 1M input, $0.28 per 1M output (cache read $0.0028/M). The pinned
# endpoint reports supports_implicit_caching=false, so uncached input pricing
# is exact; the conservative no-cache-credit rule stays.
OPENROUTER_MIMO_FLASH_PRICES = {"xiaomi/mimo-v2.6-flash": (140_000, 280_000)}
OPENROUTER_ENDPOINT_PIN = "xiaomi/fp8"
OPENROUTER_CONTEXT_INPUT_TOKENS = 1_048_576

# Providers whose forwarding rewrites caller request fields: their ledger
# calls carry ``shaping_applied`` so downstream accounting can treat the
# forced values, not the requested ones, as the treatment.
_SHAPED_PROVIDERS = frozenset({"mimo_selfhosted", "openrouter"})


# ---------------------------------------------------------------------------
# Provider profiles. One metered implementation, pinned per provider: env
# names, upstream host/path, admitted models, and list prices (USD per 1M
# tokens, micros). Rates are conservative: every input token is priced as
# uncached prefill; cached-prefill discounts are never credited.
# ---------------------------------------------------------------------------

# Z.ai Open Platform rates (docs.z.ai/guides/overview/pricing, verified
# 2026-09): glm-5.3-flash $0.15 in / $0.50 out; glm-5.3 $1.40 in / $4.40 out.
# Thinking Machines Tinker rates (verified 2026-09-28):
# Qwen/Qwen3.6-35B-A3B 0.54/1.335; Qwen/Qwen3.8-27B 1.86/5.595;
# Qwen/Qwen3.5-9B 0.66/1.995.
PROVIDERS: dict[str, Any] = {
    "zai_openapi": {
        "label": "Z.ai OpenAPI",
        "secret_path_envs": ("EVALLAB_ZAI_OPENAPI_SECRET_PATH", "EVALLAB_ZAI_SECRET_PATH"),
        "default_secret_path": Path("/run/secrets/evallab_zai_openapi_api_key"),
        "upstream_env": "EVALLAB_ZAI_OPENAPI_UPSTREAM",
        "default_upstream": "https://api.z.ai",
        "upstream_path": "/api/paas/v4/chat/completions",
        "https_host": "api.z.ai",
        "capability_env": "EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY",
        "expires_env": "EVALLAB_ZAI_OPENAPI_CAPABILITY_EXPIRES_AT",
        "attempt_env": "EVALLAB_ZAI_OPENAPI_ATTEMPT_ID",
        "usage_env": "EVALLAB_ZAI_OPENAPI_USAGE_FILE",
        "limit_env_prefix": "EVALLAB_ZAI_OPENAPI",
        "model_prefix": "zai/",
        "allowed_models_env": "EVALLAB_ZAI_OPENAPI_ALLOWED_MODEL",
        "default_allowed_models": frozenset({"glm-5.3-flash", "glm-5.3"}),
        "flat_input_price_env": "EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION",
        "flat_output_price_env": "EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION",
        "model_prices": {
            "glm-5.3-flash": (150_000, 500_000),
            "glm-5.3": (1_400_000, 4_400_000),
        },
        "expected_base_env": None,
        "checkpoint_models": False,
        "forwarded_fields": ("model", "messages", "tools", "tool_choice", "temperature"),
    },
    "tinker": {
        "label": "Tinker",
        "secret_path_envs": ("EVALLAB_TINKER_SECRET_PATH",),
        "default_secret_path": Path("/run/secrets/evallab_tinker_api_key"),
        "upstream_env": "EVALLAB_TINKER_UPSTREAM",
        "default_upstream": "https://tinker.thinkingmachines.dev",
        "upstream_path": "/services/tinker-prod/oai/api/v1/chat/completions",
        "https_host": "tinker.thinkingmachines.dev",
        "capability_env": "EVALLAB_TINKER_PROXY_CAPABILITY",
        "expires_env": "EVALLAB_TINKER_CAPABILITY_EXPIRES_AT",
        "attempt_env": "EVALLAB_TINKER_ATTEMPT_ID",
        "usage_env": "EVALLAB_TINKER_USAGE_FILE",
        "limit_env_prefix": "EVALLAB_TINKER",
        "model_prefix": "tinker/",
        "allowed_models_env": None,
        "default_allowed_models": frozenset(
            {"Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3.8-27B", "Qwen/Qwen3.5-9B"}
        ),
        "flat_input_price_env": None,
        "flat_output_price_env": None,
        "model_prices": {
            "Qwen/Qwen3.6-35B-A3B": (540_000, 1_335_000),
            "Qwen/Qwen3.8-27B": (1_860_000, 5_595_000),
            "Qwen/Qwen3.5-9B": (660_000, 1_995_000),
        },
        "expected_base_env": "EVALLAB_TINKER_EXPECTED_BASE",
        # ``tinker/<base>@tinker://<run>:train:<i>/sampler_weights/<step>``
        "checkpoint_models": True,
        # Tinker's chat endpoint reads reasoning_effort ("none" … "xhigh" or a
        # float in [0, 0.99]; 0.9 when omitted) and OpenAI top_p, so a harness
        # can match another route's nucleus sampling. top_k is not forwarded.
        "forwarded_fields": (
            "model",
            "messages",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "reasoning_effort",
        ),
    },
    "mimo_selfhosted": {
        "label": "Mimo self-hosted",
        "secret_path_envs": ("EVALLAB_MIMO_SELFHOSTED_SECRET_PATH",),
        "default_secret_path": Path("/run/secrets/evallab_mimo_selfhosted_api_key"),
        # No default upstream: the Modal deployment URL is operator config.
        # Unset fails closed with a clear error (see upstream_base()).
        "upstream_env": "EVALLAB_MIMO_SELFHOSTED_UPSTREAM",
        "default_upstream": None,
        "upstream_path": "/v1/chat/completions",
        # https hosts pin to one Modal label (*.modal.run or a routing-region
        # *.modal.direct; see _MIMO_SELFHOSTED_MODAL_{RUN,DIRECT}_RE);
        # fixed-name pinning does not apply.
        "https_host": None,
        "capability_env": "EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY",
        "expires_env": "EVALLAB_MIMO_SELFHOSTED_CAPABILITY_EXPIRES_AT",
        "attempt_env": "EVALLAB_MIMO_SELFHOSTED_ATTEMPT_ID",
        "usage_env": "EVALLAB_MIMO_SELFHOSTED_USAGE_FILE",
        "limit_env_prefix": "EVALLAB_MIMO_SELFHOSTED",
        "model_prefix": "selfhosted/",
        "allowed_models_env": None,
        "default_allowed_models": frozenset({"XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"}),
        "flat_input_price_env": None,
        "flat_output_price_env": None,
        # Self-hosted tokens have no per-token price. Modal bills the server
        # container by time, and the lab accounts it with
        # mimo_selfhosted_trial_cost_usd, not with this ledger.
        "model_prices": {"XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B": (0, 0)},
        "expected_base_env": None,
        "checkpoint_models": False,
        # Base passthrough; the MiMo branch below forces the generation_config
        # (enable_thinking, temperature, top_p, top_k) and strips
        # reasoning_effort on top of these.
        "forwarded_fields": (
            "model",
            "messages",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "top_k",
            "chat_template_kwargs",
        ),
    },
    "openrouter": {
        "label": "OpenRouter",
        "secret_path_envs": ("EVALLAB_OPENROUTER_SECRET_PATH",),
        "default_secret_path": Path("/run/secrets/evallab_openrouter_api_key"),
        "upstream_env": "EVALLAB_OPENROUTER_UPSTREAM",
        "default_upstream": "https://openrouter.ai",
        "upstream_path": "/api/v1/chat/completions",
        "https_host": "openrouter.ai",
        "capability_env": "EVALLAB_OPENROUTER_PROXY_CAPABILITY",
        "expires_env": "EVALLAB_OPENROUTER_CAPABILITY_EXPIRES_AT",
        "attempt_env": "EVALLAB_OPENROUTER_ATTEMPT_ID",
        "usage_env": "EVALLAB_OPENROUTER_USAGE_FILE",
        "limit_env_prefix": "EVALLAB_OPENROUTER",
        # ``openrouter-metered/`` — NOT ``openrouter/``: litellm's
        # get_llm_provider routes that prefix to its own OpenRouter provider
        # and would bypass the openai-compatible path this proxy serves. With
        # the metered prefix and litellm_provider "openai" the selector
        # resolves to provider "openai" (verified 2026-09-29).
        "model_prefix": "openrouter-metered/",
        "allowed_models_env": None,
        "default_allowed_models": frozenset({"xiaomi/mimo-v2.6-flash"}),
        "flat_input_price_env": None,
        "flat_output_price_env": None,
        "model_prices": dict(OPENROUTER_MIMO_FLASH_PRICES),
        "expected_base_env": None,
        "checkpoint_models": False,
        # The openrouter branch below forces the provider/reasoning pins and
        # strips caller-supplied provider/reasoning/reasoning_effort on top
        # of these.
        "forwarded_fields": ("model", "messages", "temperature", "top_p"),
    },
}

PROVIDER_ENV = "EVALLAB_PROXY_PROVIDER"


def _provider_name() -> str:
    name = os.environ.get(PROVIDER_ENV, "zai_openapi")
    if name not in PROVIDERS:
        raise ValueError(f"unknown proxy provider {name!r}")
    return name


def _profile() -> dict[str, Any]:
    return PROVIDERS[_provider_name()]

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

STRIP_INBOUND_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-evallab-proxy-capability",
        "x-evallab-proxy-nonce",
        "host",
        "content-length",
        "accept-encoding",
        "te",
    }
)


def secret_path() -> Path:
    profile = _profile()
    raw = ""
    for name in profile["secret_path_envs"]:
        raw = os.environ.get(name) or raw
    return Path(raw) if raw else profile["default_secret_path"]


def upstream_base() -> str:
    profile = _profile()
    raw = os.environ.get(profile["upstream_env"], profile.get("default_upstream"))
    if not raw:
        raise RuntimeError(
            f"{profile['label']} upstream is not configured: "
            f"set {profile['upstream_env']} to the pinned chat-completions base URL"
        )
    return raw.rstrip("/")


#: Admitted https hosts for the self-hosted MiMo route, full match only:
#: one Modal serving label under ``*.modal.run`` (Web Functions) or under a
#: documented routing region ``*.modal.direct`` (Servers, e.g.
#: ``https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct``).
#: ``modal.run.evil.com``, ``x.evil.modal.direct`` (not a routing region),
#: ``modal.direct.evil.com`` and multi-label ``a.b.us-east.modal.direct``
#: never match.
_MIMO_SELFHOSTED_MODAL_RUN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.modal\.run$"
)
_MIMO_SELFHOSTED_MODAL_DIRECT_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:us-east|us-west|ca-central|eu-west|ap-south|ap-southeast-2)\.modal\.direct$"
)


def _env(suffix: str) -> str:
    return f"{_profile()['limit_env_prefix']}_{suffix}"


def provider_key() -> str:
    label = _profile()["label"]
    path = secret_path()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{label} secret file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{label} secret file is unavailable")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError(f"{label} secret file is unavailable")
        if (info.st_mode & 0o777) not in {0o400, 0o600}:
            raise RuntimeError(f"{label} secret file is unavailable")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    value = b"".join(chunks).decode("utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError(f"{label} secret file is empty")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, msg
        raise urllib.error.HTTPError(newurl, code, "redirects disabled", headers, None)


def _pinned_upstream_url() -> str:
    profile = _profile()
    https_host = profile["https_host"]
    upstream_path = profile["upstream_path"]
    parsed = urllib.parse.urlsplit(upstream_base())
    if profile.get("https_host") is None and _provider_name() == "mimo_selfhosted":
        # Self-hosted route: https pins to one Modal label (*.modal.run Web
        # Functions, or a documented routing region *.modal.direct Servers)
        # on 443 with no userinfo, path, query, or fragment. Loopback http
        # stays for tests.
        if parsed.scheme == "https":
            if parsed.username is not None or parsed.password is not None:
                raise RuntimeError("upstream userinfo is not pinned")
            host = parsed.hostname or ""
            if not (
                _MIMO_SELFHOSTED_MODAL_RUN_RE.fullmatch(host)
                or _MIMO_SELFHOSTED_MODAL_DIRECT_RE.fullmatch(host)
            ):
                raise RuntimeError("upstream host is not pinned")
            port = parsed.port or 443
            if port != 443:
                raise RuntimeError("upstream port is not pinned")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise RuntimeError("upstream path is not pinned")
            return f"https://{host}:443{upstream_path}"
        if parsed.scheme == "http":
            host = parsed.hostname
            if not host or host not in ALLOWED_HTTP_HOSTS:
                raise RuntimeError("http upstream is not pinned")
            port = parsed.port
            if port is None:
                raise RuntimeError("http upstream port is not pinned")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise RuntimeError("upstream path is not pinned")
            return f"http://{host}:{port}{upstream_path}"
        raise RuntimeError("upstream scheme is not pinned")
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            # Credentials ride the Authorization header the proxy injects,
            # never the upstream URL.
            raise RuntimeError("upstream userinfo is not pinned")
        if parsed.hostname != https_host:
            raise RuntimeError("upstream host is not pinned")
        port = parsed.port or 443
        if port != 443:
            raise RuntimeError("upstream port is not pinned")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("upstream path is not pinned")
        return f"https://{https_host}:443{upstream_path}"
    if parsed.scheme == "http":
        host = parsed.hostname
        if not host or host not in ALLOWED_HTTP_HOSTS:
            raise RuntimeError("http upstream is not pinned")
        port = parsed.port
        if port is None:
            raise RuntimeError("http upstream port is not pinned")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("upstream path is not pinned")
        return f"http://{host}:{port}{upstream_path}"
    raise RuntimeError("upstream scheme is not pinned")

def _key_needles(key: str) -> tuple[bytes, ...]:
    utf8 = key.encode("utf-8")
    if not utf8:
        return ()
    escaped = json.dumps(key, ensure_ascii=True)[1:-1].encode("ascii")
    raw_escaped = json.dumps(key, ensure_ascii=False)[1:-1].encode("utf-8")
    b64 = base64.b64encode(utf8)
    url_quoted = urllib.parse.quote(key).encode("ascii")
    needles = {
        utf8,
        escaped,
        raw_escaped,
        url_quoted,
        key.encode("unicode_escape"),
        key.encode("utf-16le"),
        key.encode("utf-16be"),
        b64,
        b64.rstrip(b"="),
        b"Bearer " + utf8,
        b"bearer " + utf8,
        b"BEARER " + utf8,
    }
    return tuple(needle for needle in needles if needle)


def _redact_key(data: bytes, key: str) -> bytes:
    redacted = data
    for needle in _key_needles(key):
        redacted = redacted.replace(needle, b"<redacted>")
    return redacted


def _scrub_json(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return _redact_key(value.encode("utf-8"), key).decode("utf-8")
    if isinstance(value, list):
        return [_scrub_json(item, key) for item in value]
    if isinstance(value, dict):
        return {
            _redact_key(str(name).encode("utf-8"), key).decode("utf-8"): _scrub_json(item, key)
            for name, item in value.items()
        }
    return value


def _canonicalize_and_redact_json(data: bytes, key: str) -> bytes:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("unsupported upstream body") from exc

    canonical = json.dumps(
        _scrub_json(payload, key), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    sanitized = _redact_key(canonical, key)
    for needle in _key_needles(key):
        if needle in sanitized:
            raise ValueError("reflected secret remains")
    return sanitized


def _canonicalize_sse_and_usage(
    data: bytes, key: str
) -> tuple[bytes, dict[str, int] | None, str | None]:
    """Redact a text/event-stream and extract usage from the final data event.

    Returns the sanitized SSE body, a usage dict with ``prompt_tokens`` and
    ``completion_tokens``, and the observed model identity string.
    """
    usage: dict[str, int] | None = None
    returned_models: set[str] = set()
    events: list[list[bytes]] = []
    current: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if line in (b"\n", b"\r\n"):
            if current:
                events.append(current)
                current = []
        else:
            current.append(line.rstrip(b"\r\n"))
    if current:
        events.append(current)

    sanitized_events: list[bytes] = []
    for event in events:
        data_lines: list[bytes] = []
        other_lines: list[bytes] = []
        for line in event:
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
            else:
                other_lines.append(line)

        if not data_lines:
            sanitized_other = [_redact_key(line, key) for line in other_lines]
            for line in sanitized_other:
                for needle in _key_needles(key):
                    if needle in line:
                        raise ValueError("reflected secret remains")
            sanitized_events.append(b"\n".join(sanitized_other))
            continue

        raw_data = b"\n".join(data_lines)
        if raw_data.strip() == b"[DONE]":
            sanitized_lines = [_redact_key(line, key) for line in other_lines]
            sanitized_lines.append(b"data: [DONE]")
            sanitized_events.append(b"\n".join(sanitized_lines))
            continue

        try:
            payload = json.loads(raw_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("unsupported upstream body") from exc

        event_model = payload.get("model")
        if event_model is not None:
            if "<redacted>" in json.dumps(event_model):
                event_model = None
            if event_model is not None:
                returned_models.add(str(event_model))
        event_usage = payload.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage

        canonical = json.dumps(
            _scrub_json(payload, key), ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
        canonical = _redact_key(canonical, key)
        for needle in _key_needles(key):
            if needle in canonical:
                raise ValueError("reflected secret remains")

        sanitized_lines = [_redact_key(line, key) for line in other_lines]
        sanitized_lines.append(b"data: " + canonical)
        sanitized_events.append(b"\n".join(sanitized_lines))

    body = b"\n\n".join(sanitized_events)
    if body:
        body += b"\n\n"
    if len(returned_models) > 1:
        raise ValueError("upstream SSE events disagree on model identity")
    return _redact_key(body, key), usage, next(iter(returned_models), None)


def _response_encoding_ok(headers: http.client.HTTPMessage, *, stream: bool = False) -> bool:
    encoding = (headers.get("Content-Encoding") or "identity").strip().casefold()
    transfer = (headers.get("Transfer-Encoding") or "identity").strip().casefold()
    if encoding not in {"identity", ""}:
        return False
    if transfer not in {"identity", "chunked", ""}:
        return False
    content_type = headers.get("Content-Type") or ""
    media, _, params = content_type.partition(";")
    if stream:
        if media.strip().casefold() not in {"application/json", "text/event-stream"}:
            return False
    else:
        if media.strip().casefold() not in {"application/json"}:
            return False
    charset = "utf-8"
    for part in params.split(";"):
        name, _, value = part.strip().partition("=")
        if name.casefold() == "charset" and value:
            charset = value.strip().strip('"').casefold()
    return charset in {"utf-8", "us-ascii", ""}


def _capability_ok(presented: str) -> bool:
    expected = os.environ.get(_profile()["capability_env"], "")
    if not expected or not presented:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def _expired() -> bool:
    raw = os.environ.get(_profile()["expires_env"])
    if raw is None or raw == "":
        return False
    try:
        deadline = float(raw)
    except ValueError:
        return True
    return time.time() >= deadline


def _int_env(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is not None:
            return default
        raise ValueError(name)
    value = int(raw)
    if value < 0:
        raise ValueError(name)
    return value


def _estimate_tokens(payload: dict[str, Any]) -> int:
    """Reserve a conservative upper bound. Never trust characters/4."""
    billed = {
        "messages": payload.get("messages"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
    }
    encoded = json.dumps(billed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(1, len(encoded))


def _cost_micros(
    input_tokens: int, output_tokens: int, rates: tuple[int, int]
) -> int:
    numerator = input_tokens * rates[0] + output_tokens * rates[1]
    return (numerator + 999_999) // 1_000_000


def _model_rates(native_base: str) -> tuple[int, int] | None:
    """Resolve the pinned (input, output) micros per 1M tokens for one base."""
    profile = _profile()
    input_env = profile["flat_input_price_env"]
    output_env = profile["flat_output_price_env"]
    if input_env is not None and output_env is not None:
        raw_input = os.environ.get(input_env)
        raw_output = os.environ.get(output_env)
        if raw_input is not None and raw_output is not None:
            try:
                return (int(raw_input), int(raw_output))
            except ValueError:
                return None
    return profile["model_prices"].get(native_base)


def _allowed_native_models() -> frozenset[str]:
    profile = _profile()
    allowed_env = profile["allowed_models_env"]
    if allowed_env is not None:
        raw = os.environ.get(allowed_env, "")
        if raw:
            return frozenset(
                model.strip() for model in raw.split(",") if model.strip()
            )
    return profile["default_allowed_models"]


_TINKER_CHECKPOINT_RE = re.compile(
    r"^tinker://[A-Za-z0-9][A-Za-z0-9._-]*:train:\d+/sampler_weights/\d+$"
)


def _validate_model(model: Any) -> tuple[str, str, tuple[int, int]] | None:
    """Resolve the requested model to (native id, base, pinned rates).

    Z.ai: the selector must be ``zai/<id>`` with ``<id>`` in the admitted set.
    Tinker: the selector is ``tinker/<base>`` for base weights or
    ``tinker/<base>@tinker://<run>:train:<i>/sampler_weights/<step>`` for a
    fine-tuned checkpoint; the base must be admitted, must match the
    runner-pinned expected base, and the checkpoint form is checked strictly.
    """
    profile = _profile()
    prefix = profile["model_prefix"]
    if not isinstance(model, str) or not model.startswith(prefix):
        return None
    remainder = model[len(prefix) :]
    base, separator, checkpoint = remainder.partition("@")
    if separator and not profile["checkpoint_models"]:
        return None
    if separator and not _TINKER_CHECKPOINT_RE.fullmatch(checkpoint):
        return None
    if base not in _allowed_native_models() or base not in profile["model_prices"]:
        return None
    expected_env = profile["expected_base_env"]
    if expected_env is not None:
        expected = os.environ.get(expected_env, "")
        if expected != base:
            return None
    rates = _model_rates(base)
    if rates is None:
        return None
    native = checkpoint if separator else base
    return native, base, rates


class TrialBudget:
    """Concurrency-safe, durable accounting for one trial capability."""

    def __init__(self) -> None:
        profile = _profile()
        capability = os.environ.get(profile["capability_env"], "")
        attempt_id = os.environ.get(profile["attempt_env"], "")
        usage_path = os.environ.get(profile["usage_env"], "")
        if not capability or not attempt_id or not usage_path:
            raise ValueError("proxy capability accounting is not configured")
        self._lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_micros = 0
        self._nonces: set[bytes] = set()
        self._calls: list[dict[str, Any]] = []
        self._sequence = 0
        self._path = Path(usage_path)
        self._attempt_id = attempt_id
        self._capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
        self._limits = {
            "max_requests": _int_env(_env("MAX_REQUESTS")),
            "max_input_tokens": _int_env(_env("MAX_INPUT_TOKENS")),
            "max_output_tokens": _int_env(_env("MAX_OUTPUT_TOKENS")),
            "max_total_tokens": _int_env(_env("MAX_TOTAL_TOKENS")),
            "max_cost_micros": _int_env(_env("MAX_COST_MICROS")),
        }
        # A trial binds exactly one model, so its pinned rates are frozen at
        # the first reservation and every later call must agree. A zero-call
        # ledger keeps ``pricing: null``: no rate is invented without a call.
        self._pricing: dict[str, int] | None = None
        self._persist_locked()

    def _freeze_pricing_locked(self, rates: tuple[int, int]) -> None:
        if self._pricing is None:
            self._pricing = {
                "input_cost_micros_per_million": rates[0],
                "output_cost_micros_per_million": rates[1],
            }
        elif self._pricing != {
            "input_cost_micros_per_million": rates[0],
            "output_cost_micros_per_million": rates[1],
        }:
            raise ValueError("proxy pricing changed mid-trial")

    def _persist_locked(self) -> None:
        # Schema v2: ``totals`` is actual usage only (reconciled + exceeded
        # actuals); reservations of calls still reserved/unresolved sit in
        # ``attempted`` and never inflate the totals. Enforcement counters
        # (self._requests/...) still include in-flight reservations so
        # concurrent calls cannot overshoot the ceilings; only the reported
        # blocks are derived here from the calls list.
        used_requests = 0
        used_input = 0
        used_output = 0
        used_cost = 0
        attempted_requests = 0
        attempted_input = 0
        attempted_output = 0
        attempted_cost = 0
        unresolved = 0
        for call in self._calls:
            state = call["state"]
            if state in ("reconciled", "exceeded"):
                used_requests += 1
                used_input += call["input_tokens"]
                used_output += call["output_tokens"]
                used_cost += call["cost_micros"]
                if state != "reconciled":
                    unresolved += 1
            elif state in ("reserved", "unresolved"):
                attempted_requests += 1
                attempted_input += call["reserved_input_tokens"]
                attempted_output += call["reserved_output_tokens"]
                attempted_cost += call["reserved_cost_micros"]
                unresolved += 1
            else:  # pragma: no cover - states are set only by the methods below
                raise ValueError(f"invalid proxy call state: {state!r}")
        payload = {
            "schema_version": 2,
            "capability_id": self._capability_id,
            "attempt_id": self._attempt_id,
            "sequence": self._sequence,
            "limits": self._limits,
            "pricing": self._pricing,
            "totals": {
                "requests": used_requests,
                "input_tokens": used_input,
                "output_tokens": used_output,
                "total_tokens": used_input + used_output,
                "cost_micros": used_cost,
            },
            "attempted": {
                "requests": attempted_requests,
                "input_tokens": attempted_input,
                "output_tokens": attempted_output,
                "cost_micros": attempted_cost,
            },
            "unresolved_requests": unresolved,
            "calls": self._calls,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with open(descriptor, "wb", closefd=True) as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        os.replace(temporary, self._path)

    def consume_nonce(self, nonce: bytes) -> bool:
        with self._lock:
            if nonce in self._nonces:
                return False
            self._nonces.add(nonce)
            return True

    def reserve(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        requested_model: str,
        rates: tuple[int, int],
        shaping_applied: bool = False,
    ) -> int | None:
        with self._lock:
            self._freeze_pricing_locked(rates)
            next_requests = self._requests + 1
            next_input = self._input_tokens + input_tokens
            next_output = self._output_tokens + output_tokens
            next_total = next_input + next_output
            next_cost = self._cost_micros + cost_micros
            if (
                next_requests > self._limits["max_requests"]
                or next_input > self._limits["max_input_tokens"]
                or next_output > self._limits["max_output_tokens"]
                or next_total > self._limits["max_total_tokens"]
                or next_cost > self._limits["max_cost_micros"]
            ):
                return None
            self._requests = next_requests
            self._input_tokens = next_input
            self._output_tokens = next_output
            self._cost_micros = next_cost
            call_id = len(self._calls) + 1
            self._calls.append(
                {
                    "call_id": call_id,
                    "state": "reserved",
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_cost_micros": cost_micros,
                    "requested_model": requested_model,
                    **({"shaping_applied": True} if shaping_applied else {}),
                }
            )
            self._sequence += 1
            self._persist_locked()
            return call_id

    def reconcile(
        self,
        *,
        call_id: int,
        used_input: int,
        used_output: int,
        used_cost: int,
        status: int,
        returned_model: str | None = None,
        returned_model_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            delta_input = used_input - call["reserved_input_tokens"]
            delta_output = used_output - call["reserved_output_tokens"]
            delta_cost = used_cost - call["reserved_cost_micros"]
            self._input_tokens += delta_input
            self._output_tokens += delta_output
            self._cost_micros += delta_cost
            call.update(
                {
                    "state": "reconciled",
                    "status": status,
                    "input_tokens": used_input,
                    "output_tokens": used_output,
                    "cost_micros": used_cost,
                    "returned_model": returned_model,
                    "returned_model_reason": returned_model_reason,
                    **({"error": error} if error is not None else {}),
                }
            )
            self._sequence += 1
            self._persist_locked()

    def mark_unresolved(
        self,
        *,
        call_id: int,
        reason: str,
        returned_model: str | None = None,
        returned_model_reason: str | None = None,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            call.update(
                {
                    "state": "unresolved",
                    "reason": reason,
                    "returned_model": returned_model,
                    "returned_model_reason": returned_model_reason,
                }
            )
            self._sequence += 1
            self._persist_locked()

    def mark_in_flight_unresolved(self, *, reason: str) -> None:
        """Fail closed on every call still reserved: its usage is unknown."""
        with self._lock:
            in_flight = [call for call in self._calls if call["state"] == "reserved"]
            if not in_flight:
                return
            for call in in_flight:
                call.update({"state": "unresolved", "reason": reason})
                self._sequence += 1
            self._persist_locked()

    def mark_exceeded(
        self,
        *,
        call_id: int,
        reason: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            delta_input = input_tokens - call["reserved_input_tokens"]
            delta_output = output_tokens - call["reserved_output_tokens"]
            delta_cost = cost_micros - call["reserved_cost_micros"]
            self._input_tokens += delta_input
            self._output_tokens += delta_output
            self._cost_micros += delta_cost
            call.update(
                {
                    "state": "exceeded",
                    "reason": reason,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_micros": cost_micros,
                }
            )
            self._sequence += 1
            self._persist_locked()

    def remaining_output(self) -> int:
        with self._lock:
            return max(0, self._limits["max_output_tokens"] - self._output_tokens)


class ProxyServer(ThreadingHTTPServer):
    """Threading HTTPServer with bounded concurrent worker semaphore acquired before thread spawn."""

    budget: TrialBudget

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],  # noqa: N803
        max_workers: int = MAX_CONCURRENT_WORKERS,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.semaphore = threading.BoundedSemaphore(max_workers)
        self._in_flight = 0
        self._idle = threading.Condition()

    def process_request(self, request: Any, client_address: Any) -> None:
        """Acquire worker permit before spawning thread; reject 503 if capacity exceeded."""
        if not self.semaphore.acquire(blocking=False):
            body = b"proxy worker capacity exceeded\n"
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            with contextlib.suppress(OSError):
                request.settimeout(1.0)
                request.sendall(response)
            self.close_request(request)
            return

        thread = threading.Thread(
            target=self._bounded_process_request,
            args=(request, client_address),
            daemon=True,
        )
        with self._idle:
            self._in_flight += 1
        thread.start()

    def _bounded_process_request(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.close_request(request)
            self.semaphore.release()
            with self._idle:
                self._in_flight -= 1
                self._idle.notify_all()

    def drain(self, timeout: float = SHUTDOWN_DRAIN_SECONDS) -> None:
        """Stop accepting and let in-flight calls settle; fail closed after ``timeout``.

        Call after ``serve_forever`` has returned. Each handler reconciles its
        call before writing the reply, so a call whose client has gone still
        records its real usage.
        """
        self.server_close()
        with self._idle:
            settled = self._idle.wait_for(lambda: self._in_flight == 0, timeout)
        if not settled:
            self.budget.mark_in_flight_unresolved(reason="in_flight_at_shutdown")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _force_close_socket(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.connection.close()

    def _cancel_inbound_timer(self) -> None:
        if hasattr(self, "_inbound_timer") and self._inbound_timer is not None:
            self._inbound_timer.cancel()
            self._inbound_timer = None

    def setup(self) -> None:
        super().setup()
        with contextlib.suppress(AttributeError, OSError):
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        self._inbound_timer: threading.Timer | None = threading.Timer(
            REQUEST_TIMEOUT_SECONDS, self._force_close_socket
        )
        self._inbound_timer.daemon = True
        self._inbound_timer.start()

    def finish(self) -> None:
        self._cancel_inbound_timer()
        super().finish()

    def _reject(self, status: int, message: bytes) -> None:
        self._cancel_inbound_timer()
        with contextlib.suppress(OSError):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802
        self._cancel_inbound_timer()
        path = self.path.partition("?")[0]
        if path == HEALTHZ_PATH:
            body = b"ok\n"
            with contextlib.suppress(OSError):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        self._reject(404, b"endpoint not allowed\n")

    def _presented_capability(self) -> str:
        header = self.headers.get("Authorization", "")
        prefix = "bearer "
        if header.casefold().startswith(prefix):
            return header[len(prefix) :].strip()
        return self.headers.get("X-Evallab-Proxy-Capability", "").strip()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path not in _allowed_inbound_paths():
            self._reject(404, b"endpoint not allowed\n")
            return

        # AUTHENTICATE HEADERS BEFORE READING BODY (slow-body DoS protection)
        if _expired() or not _capability_ok(self._presented_capability()):
            self._reject(401, b"capability rejected\n")
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "-1")
        except ValueError:
            self._reject(400, b"invalid content length\n")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._reject(413, b"request body rejected\n")
            return

        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk_size = min(remaining, 65536)
            try:
                chunk = self.rfile.read(chunk_size)
            except (TimeoutError, OSError):
                self._reject(408, b"request body timeout\n")
                return
            if not chunk:
                self._reject(400, b"incomplete request body\n")
                return
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

        self._cancel_inbound_timer()
        with contextlib.suppress(AttributeError, OSError):
            self.connection.settimeout(UPSTREAM_TIMEOUT_SECONDS + 30.0)

        self._proxy(body)

    def _read_upstream_body(self, response: Any) -> bytes | None:
        content_length_hdr = response.headers.get("Content-Length")
        declared_len: int | None = None
        if content_length_hdr is not None:
            try:
                declared_len = int(content_length_hdr)
                if declared_len < 0 or declared_len > MAX_RESPONSE_BYTES:
                    return None
            except ValueError:
                return None

        chunks: list[bytes] = []
        total_read = 0
        limit = MAX_RESPONSE_BYTES
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > limit:
                    return None
                chunks.append(chunk)
        except (OSError, http.client.HTTPException):
            return None

        if declared_len is not None and total_read != declared_len:
            return None

        return b"".join(chunks)

    def _budget(self) -> TrialBudget:
        server = self.server
        assert isinstance(server, ProxyServer)
        return server.budget

    def _proxy(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, b"invalid json\n")
            return
        if not isinstance(payload, dict):
            self._reject(400, b"invalid json\n")
            return

        nonce = (self.headers.get("X-Evallab-Proxy-Nonce") or "").encode("utf-8")
        if nonce and not self._budget().consume_nonce(nonce):
            self._reject(409, b"replay rejected\n")
            return

        resolved = _validate_model(payload.get("model"))
        if resolved is None:
            self._reject(403, b"model not allowed\n")
            return
        model, _base, rates = resolved
        full_model = str(payload["model"])
        requested_stream = payload.get("stream", False)
        if not isinstance(requested_stream, bool):
            self._reject(400, b"invalid stream field\n")
            return

        try:
            input_tokens = _estimate_tokens(payload)
            max_output = _int_env(_env("MAX_OUTPUT_TOKENS"))
            requested_output = payload.get("max_tokens")
            if requested_output is None or int(requested_output) <= 0:
                output_tokens = max_output
            else:
                output_tokens = min(int(requested_output), max_output)
            remaining_output = self._budget().remaining_output()
            output_tokens = min(output_tokens, remaining_output)
            if output_tokens <= 0:
                self._reject(429, b"trial budget exhausted\n")
                return
            cost = _cost_micros(input_tokens, output_tokens, rates)
        except (TypeError, ValueError):
            self._reject(400, b"invalid budget fields\n")
            return

        try:
            call_id = self._budget().reserve(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost,
                requested_model=full_model,
                rates=rates,
                shaping_applied=_provider_name() in _SHAPED_PROVIDERS,
            )
        except (OSError, ValueError):
            self._reject(503, b"budget accounting unavailable\n")
            return
        if call_id is None:
            self._reject(429, b"trial budget exhausted\n")
            return

        try:
            key = provider_key()
        except RuntimeError:
            self._budget().reconcile(
                call_id=call_id,
                used_input=0,
                used_output=0,
                used_cost=0,
                status=0,
            )
            self._reject(500, b"provider secret unavailable\n")
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in HOP_BY_HOP and name.casefold() not in STRIP_INBOUND_HEADERS
        }

        forwarded = {
            name: payload[name]
            for name in _profile()["forwarded_fields"]
            if name in payload
        }
        forwarded["model"] = model
        if "max_tokens" in payload and payload["max_tokens"] is not None:
            forwarded["max_tokens"] = min(int(payload["max_tokens"]), output_tokens)
        else:
            forwarded["max_tokens"] = output_tokens
        forwarded["n"] = 1
        forwarded["stream"] = requested_stream
        if requested_stream:
            forwarded["stream_options"] = {"include_usage": True}
        if _provider_name() == "mimo_selfhosted":
            # Proxy-enforced generation_config for the self-hosted MiMo
            # route only. SGLang's ``mimo`` reasoning parser only splits
            # ``<think>`` when enable_thinking=True; without it reasoning
            # lands in content and breaks Terminus JSON. Values mirror
            # MIMO_SELFHOSTED_* in execution_contracts.py (this standalone
            # script cannot import it). reasoning_effort — e.g. the HAR-81
            # student's "none", top-level or inside chat_template_kwargs —
            # would map into thinking modes that silently disable MiMo
            # thinking, so it is stripped in both places.
            forwarded.pop("reasoning_effort", None)
            template = forwarded.get("chat_template_kwargs")
            template = dict(template) if isinstance(template, dict) else {}
            template.pop("reasoning_effort", None)
            template["enable_thinking"] = True
            forwarded["chat_template_kwargs"] = template
            forwarded["temperature"] = 0.6
            forwarded["top_p"] = 0.95
            forwarded["top_k"] = 20
        if _provider_name() == "openrouter":
            # Proxy-enforced request shaping for the OpenRouter route only.
            # The provider pin (endpoint tag xiaomi/fp8) fixes which upstream
            # serves and prices the call — OpenRouter's fallback pool is
            # refused — and reasoning stays enabled, matching the self-hosted
            # MiMo treatment. Values mirror OPENROUTER_* in
            # execution_contracts.py (this standalone script cannot import
            # it). Caller-supplied provider/reasoning/reasoning_effort — any
            # of which could reroute or silently disable thinking — are
            # stripped first.
            forwarded.pop("provider", None)
            forwarded.pop("reasoning", None)
            forwarded.pop("reasoning_effort", None)
            forwarded["provider"] = dict(OPENROUTER_PROVIDER_PIN)
            forwarded["reasoning"] = dict(OPENROUTER_REASONING_PIN)

        forwarded_body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(forwarded_body))
        headers["Accept-Encoding"] = "identity"
        headers["TE"] = "identity"

        try:
            target = _pinned_upstream_url()
        except RuntimeError:
            self._budget().reconcile(
                call_id=call_id,
                used_input=0,
                used_output=0,
                used_cost=0,
                status=0,
            )
            self._reject(502, b"provider unavailable\n")
            return

        request = urllib.request.Request(
            target,
            data=forwarded_body,
            headers=headers,
            method="POST",
        )
        context = ssl.create_default_context() if target.startswith("https://") else None
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        else:
            handlers.append(urllib.request.HTTPHandler())
        opener = urllib.request.build_opener(*handlers)

        try:
            response = opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="upstream_redirect",
                )
                self._reject(502, b"redirects disabled\n")
                return
            response = exc
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            self._budget().mark_unresolved(
                call_id=call_id,
                reason="upstream_transport_error",
            )
            self._reject(502, b"provider unavailable\n")
            return

        with response:
            raw_status = getattr(response, "status", 502)
            status = raw_status if isinstance(raw_status, int) else 502
            if 300 <= status < 400:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="upstream_redirect",
                )
                self._reject(502, b"redirects disabled\n")
                return

            content_type = response.headers.get("Content-Type") or ""
            is_stream = "text/event-stream" in content_type.casefold()
            if not _response_encoding_ok(response.headers, stream=is_stream):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_encoding",
                )
                self._reject(502, b"unsupported upstream encoding\n")
                return

            upstream_body = self._read_upstream_body(response)
            if upstream_body is None:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_body",
                )
                self._reject(502, b"unsupported upstream body\n")
                return
            if b"\x00" in upstream_body:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_body",
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            returned_model = None
            returned_model_reason = "response_not_observed"
            usage: dict[str, int] | None = None
            try:
                if is_stream:
                    sanitized_body, usage, returned_model = _canonicalize_sse_and_usage(
                        upstream_body, key
                    )
                    returned_model_reason = (
                        "model_absent_or_null" if returned_model is None else None
                    )
                else:
                    sanitized_body = _canonicalize_and_redact_json(upstream_body, key)
                    upstream_payload = json.loads(sanitized_body.decode("ascii"))
                    if not isinstance(upstream_payload, dict):
                        raise ValueError("upstream payload is not an object")
                    returned_model = upstream_payload.get("model")
                    returned_model_reason = (
                        "model_absent_or_null" if returned_model is None else None
                    )
                    if "<redacted>" in json.dumps(returned_model):
                        returned_model = None
                        returned_model_reason = "model_redacted"
                    usage = upstream_payload.get("usage")
            except (KeyError, TypeError, ValueError):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unreconciled_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            if status == 400 and not isinstance(usage, dict):
                # The provider rejected the request before generating
                # anything (SGLang's context overflow is a usage-less 400), so
                # it used no tokens. Settle it as a zero-usage error call that
                # releases its reservation, instead of leaving it unresolved
                # and failing a trial the verifier already scored.
                self._budget().reconcile(
                    call_id=call_id,
                    used_input=0,
                    used_output=0,
                    used_cost=0,
                    status=status,
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                    error="provider_http_400_no_usage",
                )
                self._reject(status, sanitized_body)
                return

            if status >= 400 and not isinstance(usage, dict):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason=f"provider_http_{status}_usage_unknown",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(status, sanitized_body)
                return

            if not isinstance(usage, dict) or not all(
                k in usage for k in ("prompt_tokens", "completion_tokens")
            ):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unreconciled_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            used_input = usage["prompt_tokens"]
            used_output = usage["completion_tokens"]
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (used_input, used_output)
            ):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="negative_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            used_cost = _cost_micros(used_input, used_output, rates)
            if used_input > input_tokens or used_output > output_tokens or used_cost > cost:
                self._budget().mark_exceeded(
                    call_id=call_id,
                    reason="provider_usage_exceeded_reservation",
                    input_tokens=used_input,
                    output_tokens=used_output,
                    cost_micros=used_cost,
                )
                self._reject(429, b"trial budget exhausted\n")
                return

            self._budget().reconcile(
                call_id=call_id,
                used_input=used_input,
                used_output=used_output,
                used_cost=used_cost,
                status=status,
                returned_model=returned_model,
                returned_model_reason=returned_model_reason,
            )

            self.send_response(status)
            sanitized_type = _redact_key(content_type.encode("utf-8"), key).decode("utf-8")
            self.send_header("Content-Type", sanitized_type)
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Length", str(len(sanitized_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(sanitized_body)


def _write_ready_file(path: Path, host: str, port: int) -> None:
    """Record the bound loopback endpoint for a supervising host process.

    The payload carries only the address the socket bound; it never contains
    capabilities, secrets, or budget state. The parent directory must already
    exist inside a private trial directory. Published atomically via temp file
    plus rename (the same durable pattern as the budget ledger) so a
    supervisor polling on existence never observes empty or partial JSON.
    """
    payload = (json.dumps({"host": host, "port": port}, sort_keys=True) + "\n").encode(
        "ascii"
    )
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    try:
        with open(descriptor, "wb", closefd=True) as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    os.replace(temporary, target)


def serve(
    host: str = "0.0.0.0",
    port: int | None = None,
    max_workers: int = MAX_CONCURRENT_WORKERS,
    ready_file: Path | str | None = None,
) -> ProxyServer:
    bound_port = int(os.environ.get("PORT", "8080") if port is None else port)
    # Fail closed at startup on a misconfigured upstream. Providers with a
    # default always pass; providers without one (mimo_selfhosted) refuse to
    # bind until their upstream env is set, with a clear error.
    _pinned_upstream_url()
    server = ProxyServer((host, bound_port), Handler, max_workers=max_workers)
    server.budget = TrialBudget()
    if ready_file is not None:
        bound_host, bound_port_actual = server.server_address[:2]
        _write_ready_file(Path(ready_file), str(bound_host), int(bound_port_actual))
    return server


def _host_entrypoint_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the host-supervised entrypoint flags.

    Defaults preserve the container sidecar invocation exactly (bind all
    interfaces, ``PORT`` env, no ready file). The host trial supervisor passes
    explicit ``--host 127.0.0.1 --port 0 --ready-file <private path>`` so the
    loopback instance never depends on ambient ``PORT`` state.
    """
    parser = argparse.ArgumentParser(
        description="Least-privilege metered provider proxy (Z.ai OpenAPI, Tinker, self-hosted MiMo)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--ready-file", default=None)
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=None,
        help="Pin the provider profile; defaults to EVALLAB_PROXY_PROVIDER or zai_openapi",
    )
    return parser.parse_args(argv)


def _serve_until_terminated(server: ProxyServer) -> None:
    """Serve until SIGTERM, then drain in-flight calls before exiting."""
    # ``shutdown`` blocks until ``serve_forever`` returns, so it cannot run on
    # the main thread that the signal interrupts.
    signal.signal(
        signal.SIGTERM,
        lambda _signum, _frame: threading.Thread(target=server.shutdown, daemon=True).start(),
    )
    server.serve_forever()
    server.drain()


if __name__ == "__main__":
    _entry_args = _host_entrypoint_args()
    if _entry_args.provider is not None:
        os.environ[PROVIDER_ENV] = _entry_args.provider
    _provider_name()  # fail closed on an unknown provider before binding
    _serve_until_terminated(
        serve(
            host=_entry_args.host,
            port=_entry_args.port,
            ready_file=_entry_args.ready_file,
        )
    )
