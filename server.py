#!/usr/bin/env python3
"""Anthropic Bridge — OpenAI-compatible proxy over the Anthropic Messages API.

Authentication: **API keys** (Anthropic Console) or **AWS Bedrock** credentials.
Subscription OAuth (Claude Pro/Max) is intentionally NOT supported: since
February 2026 Anthropic's terms prohibit using subscription OAuth tokens in
third-party tools, and it is enforced server-side (April 2026).

Endpoints
  POST /v1/chat/completions   OpenAI format (stream, tools, vision, PDF, thinking,
                              web_search, code_execution, reasoning_content)
  POST /v1/messages           Anthropic Messages passthrough (multi-account)
  GET  /v1/models, /v1/models/<id>, /v1/quota, /v1/usage
  GET  /admin/accounts (?quota=1), POST/DELETE /admin/accounts[/<key>]
  GET  /docs (Swagger UI), /api/spec.yml, /health
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, jsonify, request

BRIDGE_VERSION = "1.0.0-dev"


# ── .env loader (no python-dotenv; `source .env` does not export) ──────────
def _load_dotenv(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


HERE = Path(__file__).resolve().parent
_DOTENV_LOADED = _load_dotenv(HERE / ".env")

# ── Logging / debug ────────────────────────────────────────────────────────
BRIDGE_DEBUG = os.environ.get("BRIDGE_DEBUG", "").lower() in ("1", "true", "yes", "on")
logging.basicConfig(level=logging.DEBUG if BRIDGE_DEBUG else logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
log = logging.getLogger("bridge")


def _log_exc(where: str) -> str:
    msg = traceback.format_exc() if BRIDGE_DEBUG else str(sys.exc_info()[1])
    log.error("%s: %s", where, msg)
    return msg


# ── Config ─────────────────────────────────────────────────────────────────
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "")           # single-account client key
BRIDGE_ADMIN_KEY = os.environ.get("BRIDGE_ADMIN_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")     # single-account upstream key
ACCOUNTS_FILE = Path(os.environ.get("BRIDGE_ACCOUNTS_FILE", str(HERE / "accounts.json")))
QUOTA_CACHE_TTL = int(os.environ.get("BRIDGE_QUOTA_TTL", "30"))
DEFAULT_MAX_TOKENS = int(os.environ.get("BRIDGE_DEFAULT_MAX_TOKENS", "8192"))
UPSTREAM_TIMEOUT = int(os.environ.get("BRIDGE_UPSTREAM_TIMEOUT", "600"))
ALLOW_PRIVATE_URLS = os.environ.get("BRIDGE_ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")
MAX_IMAGE_BYTES = int(os.environ.get("BRIDGE_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_FILE_BYTES = int(os.environ.get("BRIDGE_MAX_FILE_BYTES", str(32 * 1024 * 1024)))
# Public betas that add capabilities (all documented by Anthropic)
BETA_INTERLEAVED_THINKING = "interleaved-thinking-2025-05-14"
BETA_CONTEXT_1M = "context-1m-2025-08-07"
BETA_EFFORT = "effort-2025-11-24"
BETA_CODE_EXECUTION = "code-execution-2025-08-25"
BETA_FILES = "files-api-2025-04-14"
BETA_PDF = "pdfs-2024-09-25"
USE_CONTEXT_1M = os.environ.get("BRIDGE_CONTEXT_1M", "").lower() in ("1", "true", "yes")

# thinking budgets by reasoning_effort (Anthropic: budget_tokens < max_tokens, min 1024)
THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 32768}

app = Flask(__name__)


# ── Accounts (API key / Bedrock) ───────────────────────────────────────────
class Account:
    """One upstream credential. backend = 'anthropic' (x-api-key) or 'bedrock' (AWS SigV4)."""

    def __init__(self, api_key: str, label: str = "", backend: str = "anthropic",
                 anthropic_key: str = "", aws_region: str = "", aws_profile: str = ""):
        self.api_key = api_key            # client-facing key (Authorization: Bearer)
        self.label = label or api_key[:12]
        self.backend = backend
        self.anthropic_key = anthropic_key
        self.aws_region = aws_region or os.environ.get("AWS_REGION", "us-east-1")
        self.aws_profile = aws_profile
        self.requests = 0
        self.errors = 0
        self.last_used = 0.0
        self.last_latency_ms = 0
        self.ratelimit: dict[str, Any] = {}   # last anthropic-ratelimit-* headers seen
        self._lock = threading.Lock()

    def to_public(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key, "label": self.label, "backend": self.backend,
            "configured": bool(self.anthropic_key) if self.backend == "anthropic" else True,
            "region": self.aws_region if self.backend == "bedrock" else None,
            "stats": {"requests": self.requests, "errors": self.errors,
                      "last_used": self.last_used, "last_latency_ms": self.last_latency_ms},
        }


class AccountManager:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if ACCOUNTS_FILE.exists():
            try:
                data = json.loads(ACCOUNTS_FILE.read_text())
                for key, cfg in (data.get("accounts") or {}).items():
                    self._accounts[key] = Account(
                        key, cfg.get("label", ""), cfg.get("backend", "anthropic"),
                        cfg.get("anthropic_key", ""), cfg.get("aws_region", ""), cfg.get("aws_profile", ""))
                log.info("loaded %d account(s) from %s", len(self._accounts), ACCOUNTS_FILE)
            except Exception:
                _log_exc("accounts.json")
        if not self._accounts and (ANTHROPIC_API_KEY or os.environ.get("BRIDGE_BACKEND") == "bedrock"):
            key = BRIDGE_API_KEY or "default"
            self._accounts[key] = Account(key, "default", os.environ.get("BRIDGE_BACKEND", "anthropic"),
                                          ANTHROPIC_API_KEY, os.environ.get("AWS_REGION", ""),
                                          os.environ.get("AWS_PROFILE", ""))
            log.info("single-account mode (%s)", self._accounts[key].backend)

    def _save(self) -> None:
        data = {"accounts": {k: {"label": a.label, "backend": a.backend, "anthropic_key": a.anthropic_key,
                                 "aws_region": a.aws_region, "aws_profile": a.aws_profile}
                             for k, a in self._accounts.items()}}
        ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(ACCOUNTS_FILE, 0o600)
        except Exception:
            pass

    def get(self, key: str) -> Account | None:
        return self._accounts.get(key)

    def default(self) -> Account | None:
        if len(self._accounts) == 1:
            return next(iter(self._accounts.values()))
        return self._accounts.get(BRIDGE_API_KEY or "default")

    def add(self, key: str, **kw: Any) -> Account:
        with self._lock:
            a = Account(key, **kw)
            self._accounts[key] = a
            self._save()
            return a

    def remove(self, key: str) -> bool:
        with self._lock:
            if key in self._accounts:
                del self._accounts[key]
                self._save()
                return True
            return False

    def list(self) -> list[dict[str, Any]]:
        return [a.to_public() for a in self._accounts.values()]


accounts = AccountManager()


def _bearer() -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "").strip()


def _resolve_account() -> Account | None:
    key = _bearer()
    if key and (a := accounts.get(key)):
        return a
    if not BRIDGE_API_KEY and len(accounts._accounts) == 1:
        return accounts.default()
    return None


def _require_account():
    a = _resolve_account()
    if not a:
        if not accounts._accounts:
            return None, (jsonify({"error": {"message": "No account configured. Set ANTHROPIC_API_KEY (or accounts.json).",
                                              "type": "authentication_error"}}), 401)
        return None, (jsonify({"error": {"message": "Invalid API key. Use Authorization: Bearer <key>",
                                          "type": "authentication_error"}}), 401)
    return a, None


def _check_admin():
    if not BRIDGE_ADMIN_KEY:
        return None
    if not hmac.compare_digest(_bearer().encode(), BRIDGE_ADMIN_KEY.encode()):
        return jsonify({"error": {"message": "Admin access denied", "type": "authentication_error"}}), 401
    return None


# ── Upstream call (Anthropic or Bedrock) ───────────────────────────────────
def _anthropic_headers(account: Account, betas: list[str]) -> dict[str, str]:
    h = {"x-api-key": account.anthropic_key, "anthropic-version": ANTHROPIC_VERSION,
         "content-type": "application/json", "user-agent": f"anthropic-bridge/{BRIDGE_VERSION}"}
    if betas:
        h["anthropic-beta"] = ",".join(dict.fromkeys(betas))
    return h


_bedrock_clients: dict[str, Any] = {}


def _bedrock_client(account: Account):
    key = f"{account.aws_profile}:{account.aws_region}"
    if key not in _bedrock_clients:
        import boto3  # optional dependency
        session = boto3.Session(profile_name=account.aws_profile or None, region_name=account.aws_region)
        _bedrock_clients[key] = session.client("bedrock-runtime")
    return _bedrock_clients[key]


BEDROCK_MODEL_MAP = {
    # Anthropic id -> Bedrock model id (edit for your region / inference profiles)
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1:0",
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6-v1:0",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-v1:0",
}


def upstream_messages(account: Account, body: dict[str, Any], betas: list[str], stream: bool):
    """Returns a requests.Response-like object for Anthropic, or a Bedrock response wrapper."""
    t0 = time.time()
    account.requests += 1
    account.last_used = t0
    if account.backend == "bedrock":
        client = _bedrock_client(account)
        model = body.pop("model")
        bedrock_model = os.environ.get("BEDROCK_MODEL_" + model.replace("-", "_").replace(".", "_").upper()) \
            or BEDROCK_MODEL_MAP.get(model, model)
        body.pop("stream", None)
        body["anthropic_version"] = "bedrock-2023-05-31"
        if betas:
            body["anthropic_beta"] = list(dict.fromkeys(betas))
        if stream:
            r = client.invoke_model_with_response_stream(modelId=bedrock_model, body=json.dumps(body))
            return _BedrockStream(r["body"])
        r = client.invoke_model(modelId=bedrock_model, body=json.dumps(body))
        return _BedrockResponse(json.loads(r["body"].read()))
    url = f"{ANTHROPIC_BASE_URL}/messages"
    if BRIDGE_DEBUG:
        log.debug("upstream POST %s model=%s betas=%s body=%s", url, body.get("model"), betas, json.dumps(body)[:4000])
    r = requests.post(url, headers=_anthropic_headers(account, betas), json=body, stream=stream, timeout=UPSTREAM_TIMEOUT)
    account.last_latency_ms = int((time.time() - t0) * 1000)
    _capture_ratelimit(account, r.headers)
    if r.status_code >= 400:
        account.errors += 1
    return r


class _BedrockResponse:
    def __init__(self, data: dict[str, Any]):
        self.status_code = 200
        self._data = data
        self.headers: dict[str, str] = {}
        self.text = json.dumps(data)

    def json(self) -> dict[str, Any]:
        return self._data


class _BedrockStream:
    """Adapts Bedrock's event stream to `iter_lines()` yielding SSE-like lines."""

    def __init__(self, body):
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._body = body

    def iter_lines(self):
        for ev in self._body:
            chunk = ev.get("chunk")
            if chunk and "bytes" in chunk:
                payload = json.loads(chunk["bytes"])
                yield f"event: {payload.get('type', '')}".encode()
                yield f"data: {json.dumps(payload)}".encode()
                yield b""


def _capture_ratelimit(account: Account, headers: Any) -> None:
    rl = {k.lower(): v for k, v in headers.items() if k.lower().startswith("anthropic-ratelimit-")}
    if rl:
        rl["captured_at"] = int(time.time())
        account.ratelimit = rl


# ── Model catalog ──────────────────────────────────────────────────────────
# Static metadata by family (Anthropic's GET /v1/models only returns id/display_name/created_at).
_FAMILY_META = [
    ("claude-opus-4", {"context_window": 200000, "max_output_tokens": 128000, "supports_thinking": True, "tier": "opus"}),
    ("claude-sonnet-4", {"context_window": 200000, "max_output_tokens": 64000, "supports_thinking": True, "tier": "sonnet"}),
    ("claude-haiku-4", {"context_window": 200000, "max_output_tokens": 64000, "supports_thinking": True, "tier": "haiku"}),
    ("claude-3-7-sonnet", {"context_window": 200000, "max_output_tokens": 64000, "supports_thinking": True, "tier": "sonnet"}),
    ("claude-3-5", {"context_window": 200000, "max_output_tokens": 8192, "supports_thinking": False, "tier": "legacy"}),
    ("claude-3-", {"context_window": 200000, "max_output_tokens": 4096, "supports_thinking": False, "tier": "legacy"}),
]
_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": []}
MODELS_TTL = 3600


def _meta_for(model_id: str) -> dict[str, Any]:
    for prefix, meta in _FAMILY_META:
        if model_id.startswith(prefix):
            m = dict(meta)
            m["supports_1m_context"] = USE_CONTEXT_1M and m["tier"] in ("sonnet", "opus")
            if m["supports_1m_context"]:
                m["context_window"] = 1000000
            m["supports_images"] = True
            m["supports_pdf"] = True
            m["supports_web_search"] = True
            m["supports_code_execution"] = m["tier"] != "legacy"
            return m
    return {"context_window": 200000, "max_output_tokens": 8192, "supports_thinking": False, "tier": "unknown",
            "supports_images": True, "supports_pdf": True, "supports_web_search": True, "supports_code_execution": False}


def fetch_available_models(account: Account | None = None) -> list[dict[str, Any]]:
    if time.time() - _MODELS_CACHE["ts"] < MODELS_TTL and _MODELS_CACHE["data"]:
        return _MODELS_CACHE["data"]
    account = account or accounts.default()
    models: list[dict[str, Any]] = []
    if account and account.backend == "anthropic" and account.anthropic_key:
        try:
            r = requests.get(f"{ANTHROPIC_BASE_URL}/models?limit=100", headers=_anthropic_headers(account, []), timeout=20)
            r.raise_for_status()
            for m in r.json().get("data", []):
                created = m.get("created_at", "")
                try:
                    import datetime as _dt
                    ts = int(_dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = 0
                models.append({"id": m["id"], "object": "model", "owned_by": "anthropic", "created": ts,
                               "display_name": m.get("display_name"), **_meta_for(m["id"])})
        except Exception:
            _log_exc("fetch models")
    if not models:
        # Bedrock or offline fallback: the ids we know
        for mid in ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"):
            models.append({"id": mid, "object": "model", "owned_by": "anthropic", "created": 0,
                           "display_name": mid, **_meta_for(mid)})
    models.sort(key=lambda m: m["id"])
    _MODELS_CACHE.update(ts=time.time(), data=models)
    return models


def model_meta(model_id: str) -> dict[str, Any]:
    for m in fetch_available_models():
        if m["id"] == model_id:
            return m
    return {"id": model_id, **_meta_for(model_id)}


# ── Quota (rate-limit headers) ─────────────────────────────────────────────
def fetch_quota(account: Account, refresh: bool = False) -> dict[str, Any]:
    """Anthropic exposes remaining quota via anthropic-ratelimit-* response headers.
    We keep the last seen values; with refresh=1 (or when empty) we issue a tiny
    request to read fresh headers (costs ~10 tokens)."""
    if account.backend == "bedrock":
        return {"label": account.label, "backend": "bedrock", "note": "Bedrock has no per-key quota headers; see AWS Service Quotas",
                "stats": account.to_public()["stats"]}
    stale = not account.ratelimit or time.time() - account.ratelimit.get("captured_at", 0) > QUOTA_CACHE_TTL
    if refresh or stale:
        try:
            r = requests.post(f"{ANTHROPIC_BASE_URL}/messages", headers=_anthropic_headers(account, []),
                              json={"model": os.environ.get("BRIDGE_QUOTA_PROBE_MODEL", "claude-haiku-4-5"),
                                    "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}, timeout=20)
            _capture_ratelimit(account, r.headers)
        except Exception:
            _log_exc("quota probe")
    rl = account.ratelimit
    def g(k):
        v = rl.get(f"anthropic-ratelimit-{k}")
        try:
            return int(v) if v is not None and v.isdigit() else v
        except Exception:
            return v
    return {
        "label": account.label, "backend": "anthropic",
        "requests": {"limit": g("requests-limit"), "remaining": g("requests-remaining"), "reset": g("requests-reset")},
        "input_tokens": {"limit": g("input-tokens-limit"), "remaining": g("input-tokens-remaining"), "reset": g("input-tokens-reset")},
        "output_tokens": {"limit": g("output-tokens-limit"), "remaining": g("output-tokens-remaining"), "reset": g("output-tokens-reset")},
        "tokens": {"limit": g("tokens-limit"), "remaining": g("tokens-remaining"), "reset": g("tokens-reset")},
        "captured_at": rl.get("captured_at"),
        "stats": account.to_public()["stats"],
    }


# ── URL fetching with SSRF guard ───────────────────────────────────────────
def _assert_url_allowed(url: str) -> None:
    import ipaddress
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {u.scheme!r}")
    host = u.hostname or ""
    if not host:
        raise ValueError("URL without host")
    if ALLOW_PRIVATE_URLS:
        return
    if host in ("localhost", "metadata.google.internal") or host.endswith((".internal", ".local")):
        raise ValueError(f"blocked host: {host}")
    for info in socket.getaddrinfo(host, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"blocked address for {host}: {ip} (BRIDGE_ALLOW_PRIVATE_URLS=1 to allow)")


_EXT_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def _download_blob(url: str, hint_name: str = "", max_bytes: int = MAX_FILE_BYTES) -> tuple[str, str]:
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        return (header.split(";")[0].replace("data:", "") or "application/octet-stream"), b64
    _assert_url_allowed(url)
    r = requests.get(url, headers={"User-Agent": f"anthropic-bridge/{BRIDGE_VERSION}"}, timeout=60, stream=True)
    r.raise_for_status()
    data = r.raw.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"file too large (> {max_bytes} bytes)")
    mime = r.headers.get("Content-Type", "").split(";")[0].strip()
    if not mime or mime == "application/octet-stream":
        ext = os.path.splitext((hint_name or url.split("?")[0]).lower())[1]
        mime = _EXT_MIME.get(ext, mime or "application/octet-stream")
    return mime, base64.b64encode(data).decode("ascii")


def _download_image(url: str, timeout: int = 20) -> tuple[str, str]:
    return _download_blob(url, max_bytes=MAX_IMAGE_BYTES)


# ── OpenAI → Anthropic conversion (reused from the original bridge) ────────
def _oai_content_to_anthropic(content: str | list[Any]) -> str | list[dict[str, Any]]:
    """OpenAI content parts -> Anthropic blocks. text, image_url, file (PDF/text), input_audio (unsupported -> note)."""
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif itype == "image_url":
            image_url = item.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
            if url:
                try:
                    mime, b64 = _download_image(url)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
                except Exception as e:
                    log.warning("[vision] %s", e)
                    blocks.append({"type": "text", "text": "[image unavailable]"})
        elif itype == "file":
            spec = item.get("file", {}) or {}
            url = spec.get("file_data") or spec.get("file_url") or ""
            try:
                mime, b64 = _download_blob(url, hint_name=spec.get("filename", ""))
                if mime == "application/pdf":
                    blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                                   "title": spec.get("filename") or "document"})
                elif mime.startswith("text/"):
                    blocks.append({"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                                                   "data": base64.b64decode(b64).decode("utf-8", "replace")},
                                   "title": spec.get("filename") or "document"})
                elif mime.startswith("image/"):
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
                else:
                    blocks.append({"type": "text", "text": f"[unsupported file type {mime}]"})
            except Exception as e:
                log.warning("[files] %s", e)
                blocks.append({"type": "text", "text": "[file unavailable]"})
        elif itype == "input_audio":
            blocks.append({"type": "text", "text": "[audio input is not supported by the Anthropic Messages API]"})
    return blocks


def _oai_tool_choice_to_anthropic(tool_choice: Any) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return {"type": "none"}
        if tool_choice == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        name = tool_choice.get("function", {}).get("name") or tool_choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


# ============================================================
# JSON Schema sanitisation for Claude Tool Use
# ============================================================
# Anthropic requires JSON Schema Draft 2020-12 but only accepts a subset.
# This function strips / rewrites keywords that the upstream rejects.
# See: https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools
_UNSUPPORTED_SCHEMA_KEYS: set[str] = {
    "$schema", "$id", "$anchor", "$comment", "$defs", "definitions",
    "title", "default", "examples",
    "readOnly", "writeOnly", "deprecated",
    "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
    "patternProperties", "propertyNames", "additionalProperties",
    "dependentRequired", "dependentSchemas", "if", "then", "else", "not",
    "const", "discriminator",
}

# `format` is only useful for a very small allow-list; otherwise remove it.
_ALLOWED_STRING_FORMATS: set[str] = {"date", "date-time", "email", "uri", "uuid"}

# Constraints that can be moved into the property description so the model still
# sees them as soft guidance.
_DESCRIPTION_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("minLength", "at least {v} characters"),
    ("maxLength", "at most {v} characters"),
    ("minimum", "at least {v}"),
    ("maximum", "at most {v}"),
    ("exclusiveMinimum", "greater than {v}"),
    ("exclusiveMaximum", "less than {v}"),
    ("multipleOf", "multiple of {v}"),
    ("minItems", "at least {v} items"),
    ("maxItems", "at most {v} items"),
    ("minProperties", "at least {v} properties"),
    ("maxProperties", "at most {v} properties"),
    ("pattern", "must match pattern {v}"),
)


def _append_constraint_description(original: str, schema: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, template in _DESCRIPTION_CONSTRAINTS:
        if key in schema:
            parts.append(template.format(v=schema[key]))
    if not parts:
        return original
def _build_defs_registry(schema: dict[str, Any]) -> dict[str, Any]:
    """Collect all local definitions from $defs / definitions into a flat registry."""
    registry: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        if key in schema and isinstance(schema[key], dict):
            registry.update(schema[key])
    return registry


def _resolve_local_ref(ref: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a local JSON Schema $ref fragment using a flat definitions registry."""
    if not isinstance(ref, str):
        return None
    if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
        key = ref.split("/")[-1]
        return registry.get(key)
    return None


def _convert_const_to_enum(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert { const: x } -> { enum: [x] } so the value is not lost."""
    if "const" in schema and "enum" not in schema:
        schema = dict(schema)
        schema["enum"] = [schema.pop("const")]
    return schema


def _flatten_anyof_oneof(schema: dict[str, Any]) -> dict[str, Any]:
    """Detect const/enum-of-consts or nullable type unions in anyOf/oneOf."""
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if not isinstance(branches, list) or not branches:
            continue

        # Pattern: [{const: a}, {const: b}, ...] -> enum: [a, b]
        consts: list[Any] = []
        for b in branches:
            if isinstance(b, dict) and "const" in b:
                consts.append(b["const"])
            elif isinstance(b, dict) and isinstance(b.get("enum"), list) and len(b["enum"]) == 1:
                consts.append(b["enum"][0])
        if consts and len(consts) == len(branches):
            schema = dict(schema)
            schema.pop(key)
            schema["enum"] = consts
            return schema

        # Pattern: [{type: "string"}, {type: "null"}] -> type: "string" + nullable hint
        types = [
            b["type"]
            for b in branches
            if isinstance(b, dict) and isinstance(b.get("type"), str)
        ]
        if types and len(types) == len(branches) and "null" in types:
            schema = dict(schema)
            schema.pop(key)
            non_null = [t for t in types if t != "null"]
            if len(non_null) == 1:
                schema["type"] = non_null[0]
            elif len(non_null) > 1:
                schema["type"] = non_null[0]
                schema = _add_description_hint(schema, f"types: {', '.join(non_null)}")
            schema = _add_description_hint(schema, "nullable")
            return schema

    return schema


def _add_description_hint(schema: dict[str, Any], hint: str) -> dict[str, Any]:
    """Append an informational hint to a schema's description field."""
    existing = schema.get("description", "")
    if isinstance(existing, str) and existing:
        schema["description"] = f"{existing} ({hint})"
    else:
        schema["description"] = hint
    return schema


def _sanitize_schema_for_claude(
    schema: Any,
    in_property: bool = False,
    defs_registry: dict[str, Any] | None = None,
    _refs_stack: set[str] | None = None,
) -> Any:
    """Recursively sanitize a JSON Schema so Anthropic accepts it as input_schema.

    Handles $ref resolution across nested schemas by carrying a flat registry of
    $defs/definitions discovered at the root. Cycles are broken by a visited stack.
    """
    if not isinstance(schema, dict):
        return schema

    # On the top-level call, build the definitions registry from the schema itself.
    if defs_registry is None:
        defs_registry = _build_defs_registry(schema)
    if _refs_stack is None:
        _refs_stack = set()

    # Unwrap a nested 'schema' key if it is the only schema content.
    if "schema" in schema and isinstance(schema["schema"], dict):
        schema = dict(schema)
        schema = schema["schema"]

    # Resolve $ref first, so the rest of the function operates on the inlined target.
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in _refs_stack:
            # Cycle detected: replace with an open object to avoid infinite recursion.
            return {"type": "object", "properties": {}}
        resolved = _resolve_local_ref(ref, defs_registry)
        if isinstance(resolved, dict):
            _refs_stack.add(ref)
            inlined = _sanitize_schema_for_claude(
                resolved, in_property=in_property, defs_registry=defs_registry, _refs_stack=_refs_stack
            )
            _refs_stack.discard(ref)
            return inlined
        # External or unresolvable refs: fall through to an open object.
        return {"type": "object", "properties": {}}

    # Structural normalizations before recursive cleaning.
    schema = _convert_const_to_enum(schema)
    schema = _flatten_anyof_oneof(schema)

    # Start from a clean dict with allowed keys only.
    out: dict[str, Any] = {}

    # Preserve type first so we can reason about the node.
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) == 1:
            out["type"] = non_null[0]
        elif len(non_null) > 1:
            out["anyOf"] = [{"type": x} for x in non_null]
    elif t is not None:
        out["type"] = t

    # Keep description, but append numeric/string constraints to it so the model
    # still has a chance to respect them.
    desc = schema.get("description", "") if isinstance(schema.get("description"), str) else ""
    desc = _append_constraint_description(desc, schema)
    if desc:
        out["description"] = desc

    # Properties
    if "properties" in schema and isinstance(schema["properties"], dict):
        out["properties"] = {
            k: _sanitize_schema_for_claude(v, in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack)
            for k, v in schema["properties"].items()
        }

    # Required list: remove duplicates and invalid entries.
    if "required" in schema and isinstance(schema["required"], list):
        seen = set()
        clean_required: list[str] = []
        for r in schema["required"]:
            if isinstance(r, str) and r and r not in seen:
                seen.add(r)
                clean_required.append(r)
        if clean_required:
            out["required"] = clean_required

    # Items
    if "items" in schema:
        if isinstance(schema["items"], dict):
            out["items"] = _sanitize_schema_for_claude(
                schema["items"], in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack
            )
        elif isinstance(schema["items"], list):
            out["items"] = {
                "anyOf": [
                    _sanitize_schema_for_claude(x, in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack)
                    for x in schema["items"]
                ]
            }

    # Enum
    if "enum" in schema and isinstance(schema["enum"], list):
        out["enum"] = schema["enum"]

    # Format: only keep allowed ones.
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _ALLOWED_STRING_FORMATS:
        out["format"] = fmt

    # Combinators
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        sanitized_any = [
            _sanitize_schema_for_claude(x, in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack)
            for x in schema["anyOf"] if isinstance(x, dict)
        ]
        if sanitized_any:
            if all(len(b) == 1 and "type" in b for b in sanitized_any):
                types = [b["type"] for b in sanitized_any]
                if len(types) == 1:
                    out["type"] = types[0]
                else:
                    out["type"] = types
            else:
                out["anyOf"] = sanitized_any

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        sanitized_one = [
            _sanitize_schema_for_claude(x, in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack)
            for x in schema["oneOf"] if isinstance(x, dict)
        ]
        if sanitized_one:
            if "anyOf" in out:
                out["anyOf"].extend(sanitized_one)
            else:
                out["anyOf"] = sanitized_one

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged_props: dict[str, Any] = {}
        merged_required: list[str] = []
        for branch in schema["allOf"]:
            if not isinstance(branch, dict):
                continue
            clean_branch = _sanitize_schema_for_claude(
                branch, in_property=True, defs_registry=defs_registry, _refs_stack=_refs_stack
            )
            if isinstance(clean_branch.get("properties"), dict):
                merged_props.update(clean_branch["properties"])
            if isinstance(clean_branch.get("required"), list):
                merged_required.extend(clean_branch["required"])
        if merged_props:
            out.setdefault("properties", {}).update(merged_props)
        if merged_required:
            out.setdefault("required", []).extend(merged_required)

    # Ensure objects have a properties key.
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}

    # Remove unsupported keys that may have slipped through.
    for bad in _UNSUPPORTED_SCHEMA_KEYS:
        out.pop(bad, None)

    # If the node became empty, default to object.
    if not out:
        return {"type": "object", "properties": {}}

    return out


def _oai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        parameters = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append({
            "name": name,
            "description": fn.get("description", ""),
            "input_schema": _sanitize_schema_for_claude(parameters),
        })
    return out


def _tool_call_id_to_name(messages: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if isinstance(tc, dict):
                name = tc.get("function", {}).get("name")
                tid = tc.get("id")
                if name and tid:
                    mapping[tid] = name
        fc = msg.get("function_call")
        if isinstance(fc, dict) and fc.get("name"):
            mapping["legacy_function_call"] = fc["name"]
    return mapping


def _repair_orphan_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool_use/tool_result blocks with missing counterparts."""
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                tool_use_ids.add(block["id"])
            if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                tool_result_ids.add(block["tool_use_id"])

    orphaned_uses = tool_use_ids - tool_result_ids
    orphaned_results = tool_result_ids - tool_use_ids

    if not orphaned_uses and not orphaned_results:
        return messages

    def _filter_block(block: dict[str, Any]) -> bool:
        if block.get("type") == "tool_use" and block.get("id") in orphaned_uses:
            return False
        if block.get("type") == "tool_result" and block.get("tool_use_id") in orphaned_results:
            return False
        return True

    result = []
    for msg in messages:
        if not isinstance(msg.get("content"), list):
            result.append(msg)
            continue
        filtered = [b for b in msg["content"] if _filter_block(b)]
        if filtered:
            result.append({**msg, "content": filtered})
    return result

# thinking blocks (with signature) emitted per tool_use id, so multi-turn tool use keeps working when
# thinking is enabled: Anthropic requires the assistant's thinking block to be replayed alongside its tool_use,
# and OpenAI-format clients cannot carry the signature. Same trick as thoughtSignature in the Antigravity bridge.
_THINKING_CACHE: dict[str, dict[str, Any]] = {}
_THINKING_CACHE_MAX = 5000


def _remember_thinking(content: list[dict[str, Any]]) -> None:
    thinking_blocks = [b for b in content if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")]
    if not thinking_blocks:
        return
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
            if len(_THINKING_CACHE) > _THINKING_CACHE_MAX:
                for k in list(_THINKING_CACHE)[: _THINKING_CACHE_MAX // 5]:
                    _THINKING_CACHE.pop(k, None)
            _THINKING_CACHE[b["id"]] = thinking_blocks[0]


def _oai_messages_to_anthropic(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]] | None]:
    """OpenAI messages -> Anthropic messages + system. Handles standard `tool_calls`, `tool` results,
    content=None, consecutive same-role merging (Anthropic requires strict alternation)."""
    tool_names = _tool_call_id_to_name(messages)
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if isinstance(prev, str):
                prev = [{"type": "text", "text": prev}]
            out[-1]["content"] = prev + blocks
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.append(" ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"))
            continue
        if role == "user":
            blocks = _oai_content_to_anthropic(content or "")
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks or " "}]
            push("user", blocks or [{"type": "text", "text": " "}])
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            tool_calls = msg.get("tool_calls") or []
            # replay thinking block (with signature) if we have it for one of these tool calls
            for tc in tool_calls:
                tb = _THINKING_CACHE.get((tc or {}).get("id", ""))
                if tb:
                    blocks.append(tb)
                    break
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                        blocks.append({"type": "text", "text": item["text"]})
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                               "name": fn.get("name", "unknown"), "input": args if isinstance(args, dict) else {"value": args}})
            if not blocks:
                blocks = [{"type": "text", "text": " "}]
            push("assistant", blocks)
        elif role in ("tool", "function"):
            tid = msg.get("tool_call_id", "")
            tool_content = content
            if isinstance(content, (dict, list)):
                tool_content = json.dumps(content)
            push("user", [{"type": "tool_result", "tool_use_id": tid, "content": tool_content or " "}])

    system: str | None = "\n\n".join(p for p in system_parts if p) or None
    return out, system


def _anthropic_content_to_openai_message(content: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]], str | None]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    thinking: str | None = None
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking = (thinking or "") + block.get("thinking", "")
        elif btype == "tool_use":
            inp = block.get("input", {})
            raw_name = block.get("name", "")
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": raw_name,
                    "arguments": json.dumps(inp) if isinstance(inp, dict) else str(inp),
                },
            })
    text = "".join(text_parts) if text_parts else None
    # If the only response was a forced JSON tool, surface the JSON as text.
    if not text and len(tool_calls) == 1 and tool_calls[0]["function"]["name"].startswith("json_"):
        text = tool_calls[0]["function"]["arguments"]
        tool_calls = []
    return text, tool_calls, thinking


def _anthropic_usage_to_openai(usage: dict[str, Any]) -> dict[str, int]:
    result = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }
    # Include cache tokens if present (Anthropic prompt caching)
    if "cache_creation_input_tokens" in usage:
        result["cache_creation_tokens"] = usage["cache_creation_input_tokens"]
    if "cache_read_input_tokens" in usage:
        result["cache_read_tokens"] = usage["cache_read_input_tokens"]
    return result


# ============================================================
# API key authentication (multi-account aware)
# ============================================================


# ── Request builder (OpenAI body -> Anthropic Messages body) ───────────────
_NATIVE_TOOL_ALIASES = {
    "web_search": "web_search", "web_search_preview": "web_search", "google_search": "web_search",
    "code_execution": "code_execution", "code_interpreter": "code_execution",
}


def _split_native_tools(tools: list, body: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    native: dict[str, dict[str, Any]] = {}
    functions: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type", "function"))
        if ttype in _NATIVE_TOOL_ALIASES:
            native[_NATIVE_TOOL_ALIASES[ttype]] = t
        else:
            functions.append(t)
    if body.get("web_search") or isinstance(body.get("web_search_options"), dict):
        native.setdefault("web_search", body.get("web_search_options") or {})
    if body.get("code_execution"):
        native.setdefault("code_execution", {})
    out: list[dict[str, Any]] = []
    for name, spec in native.items():
        if name == "web_search":
            tool: dict[str, Any] = {"type": "web_search_20250305", "name": "web_search"}
            if isinstance(spec, dict):
                if spec.get("max_uses"):
                    tool["max_uses"] = int(spec["max_uses"])
                loc = spec.get("user_location")
                if isinstance(loc, dict) and loc.get("approximate"):
                    tool["user_location"] = {"type": "approximate", **{k: v for k, v in loc["approximate"].items()
                                                                       if k in ("city", "region", "country", "timezone")}}
        else:
            tool = {"type": "code_execution_20250522", "name": "code_execution"}
        out.append(tool)
    return out, functions


def build_anthropic_request(body: dict[str, Any], account: Account) -> tuple[dict[str, Any], list[str]]:
    """Returns (anthropic_body, betas)."""
    model = body.get("model", "claude-sonnet-4-6")
    if model.startswith("models/"):
        model = model[7:]
    meta = model_meta(model)
    betas: list[str] = []

    messages, system = _oai_messages_to_anthropic(body.get("messages", []))
    messages = _repair_orphan_tool_pairs(messages)

    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_MAX_TOKENS
    max_tokens = min(int(max_tokens), int(meta.get("max_output_tokens", 8192)))
    req: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}

    # system + prompt caching (cache_control on the last system block)
    if system:
        if isinstance(system, str):
            system = [{"type": "text", "text": system}]
        if body.get("prompt_caching", True) and system:
            system[-1] = {**system[-1], "cache_control": {"type": "ephemeral"}}
        req["system"] = system

    for k_oai, k_ant in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k")):
        if k_oai in body and body[k_oai] is not None:
            req[k_ant] = body[k_oai]
    if body.get("stop"):
        req["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    if body.get("stream"):
        req["stream"] = True

    # user -> metadata.user_id (Anthropic's official per-end-user field; used for abuse detection, not memory)
    user_key = str(body.get("user") or request.headers.get("X-Session-Id", "") or "").strip()
    if user_key:
        req["metadata"] = {"user_id": hashlib.sha256(f"{account.api_key}:{user_key}".encode()).hexdigest()[:64]}

    # thinking: reasoning_effort (OpenAI) or explicit thinking / thinking_budget
    effort = str(body.get("reasoning_effort") or "").lower()
    if isinstance(body.get("thinking"), dict):
        req["thinking"] = body["thinking"]
    elif body.get("thinking_budget") or effort in THINKING_BUDGETS:
        budget = int(body.get("thinking_budget") or THINKING_BUDGETS[effort])
        if meta.get("supports_thinking"):
            budget = max(1024, budget)
            if max_tokens <= budget:   # Anthropic requires max_tokens > budget_tokens
                max_tokens = min(budget + 2048, int(meta.get("max_output_tokens", 8192)))
                req["max_tokens"] = max_tokens
                budget = min(budget, max_tokens - 1024)
            req["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if "thinking" in req:
        betas.append(BETA_INTERLEAVED_THINKING)
        # thinking requires temperature 1 and no top_p/top_k
        req.pop("top_p", None); req.pop("top_k", None); req["temperature"] = 1
    if effort in ("low", "medium", "high") and meta.get("tier") in ("opus", "sonnet"):
        req["output_config"] = {"effort": effort}
        betas.append(BETA_EFFORT)

    # tools: function tools + native (web_search / code_execution)
    native_tools, function_tools = _split_native_tools(body.get("tools") or [], body)
    tools: list[dict[str, Any]] = []
    if function_tools:
        tools += _oai_tools_to_anthropic(function_tools)
    tools += native_tools
    if any(t.get("type", "").startswith("code_execution") for t in native_tools):
        betas.append(BETA_CODE_EXECUTION)

    # response_format -> forced JSON tool (works on every model)
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        schema = (rf.get("json_schema") or {}).get("schema") if rf.get("type") == "json_schema" else None
        tools.append({"name": "json_response", "description": "Respond with the JSON object.",
                      "input_schema": _sanitize_schema_for_claude(schema) if schema else {"type": "object", "additionalProperties": True}})
        req["tool_choice"] = {"type": "tool", "name": "json_response"}
    if tools:
        req["tools"] = tools
        if "tool_choice" not in req:
            tc = _oai_tool_choice_to_anthropic(body.get("tool_choice"))
            if tc:
                req["tool_choice"] = tc
    if USE_CONTEXT_1M and meta.get("supports_1m_context"):
        betas.append(BETA_CONTEXT_1M)
    if any(isinstance(b, dict) and b.get("type") == "document" for m in messages for b in (m.get("content") if isinstance(m.get("content"), list) else [])):
        betas.append(BETA_PDF)
    return req, betas


# ── Anthropic response -> OpenAI ───────────────────────────────────────────
def _extras_from_content(content: list[dict[str, Any]]) -> dict[str, Any]:
    """citations (web_search), code execution results -> extra fields."""
    extras: dict[str, Any] = {}
    cites, code_runs = [], []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            for c in block.get("citations") or []:
                if c.get("url"):
                    cites.append({"url": c.get("url"), "title": c.get("title"), "cited_text": c.get("cited_text")})
        elif block.get("type") == "web_search_tool_result":
            for r in block.get("content") or []:
                if isinstance(r, dict) and r.get("url"):
                    cites.append({"url": r["url"], "title": r.get("title")})
        elif block.get("type") == "code_execution_tool_result":
            code_runs.append(block.get("content"))
    if cites:
        seen, uniq = set(), []
        for c in cites:
            if c["url"] not in seen:
                seen.add(c["url"]); uniq.append(c)
        extras["citations"] = uniq
    if code_runs:
        extras["code_execution_results"] = code_runs
    return extras


def _render_code_blocks(content: list[dict[str, Any]]) -> str:
    """server_tool_use(code_execution) + results -> markdown appended to content."""
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "server_tool_use" and block.get("name") == "code_execution":
            code = (block.get("input") or {}).get("code", "")
            out.append(f"\n```python\n{code}\n```\n")
        elif block.get("type") == "code_execution_tool_result":
            c = block.get("content") or {}
            if isinstance(c, dict):
                stdout, stderr = c.get("stdout", ""), c.get("stderr", "")
                if stdout or stderr:
                    out.append(f"\n```\n{stdout}{stderr}\n```\n")
    return "".join(out)


_usage_stats: dict[str, dict[str, int]] = {}
_usage_lock = threading.Lock()
_START = time.time()


def _record_usage(model: str, usage: dict[str, Any], error: bool = False) -> None:
    with _usage_lock:
        s = _usage_stats.setdefault(model, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                            "cache_read_tokens": 0, "cache_creation_tokens": 0, "errors": 0})
        s["requests"] += 1
        s["errors"] += int(error)
        s["prompt_tokens"] += int(usage.get("input_tokens", 0) or 0)
        s["completion_tokens"] += int(usage.get("output_tokens", 0) or 0)
        s["cache_read_tokens"] += int(usage.get("cache_read_input_tokens", 0) or 0)
        s["cache_creation_tokens"] += int(usage.get("cache_creation_input_tokens", 0) or 0)


def _finish_reason(stop: str | None, has_tools: bool) -> str:
    if has_tools or stop == "tool_use":
        return "tool_calls"
    return {"max_tokens": "length", "end_turn": "stop", "stop_sequence": "stop"}.get(stop or "", "stop")


def _upstream_error(r, model: str):
    try:
        detail = r.json().get("error", {})
        msg = detail.get("message") or r.text
    except Exception:
        msg = r.text
    _record_usage(model, {}, error=True)
    code = getattr(r, "status_code", 502)
    return jsonify({"error": {"message": msg, "type": "upstream_error", "code": code}}), (429 if code == 429 else 502 if code >= 500 else code)


# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({"name": "anthropic-bridge", "version": BRIDGE_VERSION, "auth": "api-key / bedrock",
                    "accounts": len(accounts._accounts), "docs": "/docs", "openapi": "/api/spec.yml",
                    "endpoints": ["/health", "/v1/models", "/v1/chat/completions", "/v1/messages", "/v1/quota",
                                  "/v1/usage", "/admin/accounts", "/admin/accounts?quota=1", "/admin/accounts/<key>/quota"]})


@app.route("/health")
def health():
    if not accounts._accounts:
        return jsonify({"status": "no_account", "message": "Set ANTHROPIC_API_KEY in .env (or accounts.json)"}), 200
    a = accounts.default() or next(iter(accounts._accounts.values()))
    return jsonify({"status": "ok", "accounts": len(accounts._accounts), "default": a.to_public(),
                    "upstream": ANTHROPIC_BASE_URL, "debug": BRIDGE_DEBUG})


@app.route("/api/spec.yml")
@app.route("/openapi.yaml")
def openapi_spec():
    p = HERE / "openapi.yaml"
    if not p.exists():
        return jsonify({"error": "openapi.yaml not found"}), 404
    return Response(p.read_text(), mimetype="application/yaml")


@app.route("/docs")
def swagger_ui():
    html = """<!doctype html><html><head><meta charset="utf-8"><title>Anthropic Bridge — API docs</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css">
<style>body{margin:0;background:#fafafa}.topbar{display:none}</style></head><body><div id="swagger-ui"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js"></script>
<script>window.ui=SwaggerUIBundle({url:"/api/spec.yml",dom_id:"#swagger-ui",deepLinking:true,persistAuthorization:true,tryItOutEnabled:true,displayRequestDuration:true});</script>
</body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/v1/models")
def list_models():
    a = _resolve_account()
    return jsonify({"object": "list", "data": fetch_available_models(a)})


@app.route("/v1/models/<path:model_id>")
def get_model(model_id: str):
    for m in fetch_available_models(_resolve_account()):
        if m["id"] == model_id:
            return jsonify(m)
    return jsonify({"error": {"message": f"Model '{model_id}' not found", "type": "invalid_request_error"}}), 404


@app.route("/v1/usage")
def usage():
    with _usage_lock:
        by_model = json.loads(json.dumps(_usage_stats))
    tot = {k: sum(v[k] for v in by_model.values()) for k in ("requests", "prompt_tokens", "completion_tokens",
                                                                 "cache_read_tokens", "cache_creation_tokens", "errors")}
    return jsonify({"by_model": by_model, **{f"total_{k}": v for k, v in tot.items()},
                    "total_tokens": tot["prompt_tokens"] + tot["completion_tokens"], "uptime_seconds": int(time.time() - _START)})


@app.route("/v1/quota")
def v1_quota():
    a, err = _require_account()
    if err:
        return err
    try:
        return jsonify(fetch_quota(a, refresh=request.args.get("refresh") in ("1", "true")))
    except Exception:
        return jsonify({"error": _log_exc("/v1/quota")}), 502


@app.route("/v1/messages", methods=["POST"])
def messages_passthrough():
    """Anthropic Messages API passthrough (multi-account). Same body/response as api.anthropic.com."""
    a, err = _require_account()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("messages") or not body.get("model"):
        return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "model and messages are required"}}), 400
    betas = [b.strip() for b in request.headers.get("anthropic-beta", "").split(",") if b.strip()]
    stream = bool(body.get("stream"))
    try:
        r = upstream_messages(a, dict(body), betas, stream)
    except Exception:
        return jsonify({"type": "error", "error": {"type": "api_error", "message": _log_exc("/v1/messages")}}), 502
    if r.status_code != 200:
        return Response(r.text, status=r.status_code, mimetype="application/json")
    if stream:
        def gen():
            for line in r.iter_lines():
                yield (line.decode() if isinstance(line, bytes) else line) + "\n"
        return Response(gen(), mimetype="text/event-stream")
    data = r.json()
    _record_usage(body.get("model", "?"), data.get("usage", {}))
    return jsonify(data)


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    a, err = _require_account()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("messages"):
        return jsonify({"error": {"message": "messages is required", "type": "invalid_request_error"}}), 400
    for p in ("logprobs", "frequency_penalty", "presence_penalty", "logit_bias", "top_logprobs", "n"):
        if p in body and body[p]:
            log.warning("ignoring unsupported param %s", p)
    try:
        req, betas = build_anthropic_request(body, a)
    except Exception:
        return jsonify({"error": {"message": _log_exc("build request"), "type": "invalid_request_error"}}), 400
    model = req["model"]
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    try:
        r = upstream_messages(a, req, betas, stream)
    except Exception:
        return jsonify({"error": {"message": _log_exc("upstream"), "type": "upstream_error"}}), 502
    if r.status_code != 200:
        return _upstream_error(r, model)

    if not stream:
        data = r.json()
        content = data.get("content", [])
        _remember_thinking(content)
        text, tool_calls, thinking = _anthropic_content_to_openai_message(content)
        text = (text or "") + _render_code_blocks(content)
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if thinking:
            msg["reasoning_content"] = thinking
        msg.update(_extras_from_content(content))
        usage_a = data.get("usage", {})
        _record_usage(model, usage_a)
        return jsonify({"id": completion_id, "object": "chat.completion", "created": created, "model": model,
                        "choices": [{"index": 0, "message": msg, "finish_reason": _finish_reason(data.get("stop_reason"), bool(tool_calls))}],
                        "usage": _anthropic_usage_to_openai(usage_a)})

    def gen():
        chunk_base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model}
        def emit(delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None):
            c = {**chunk_base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage is not None:
                c["usage"] = usage
            return f"data: {json.dumps(c)}\n\n"
        yield emit({"role": "assistant"})
        blocks: dict[int, dict[str, Any]] = {}
        tool_idx = 0
        usage_acc: dict[str, Any] = {}
        stop_reason = None
        has_tools = False
        event = None
        try:
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode() if isinstance(raw, bytes) else raw
                if line.startswith("event:"):
                    event = line[6:].strip(); continue
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                et = ev.get("type") or event
                if et == "message_start":
                    usage_acc.update((ev.get("message") or {}).get("usage") or {})
                elif et == "content_block_start":
                    idx = ev.get("index", 0); cb = ev.get("content_block") or {}
                    blocks[idx] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "json": "", "thinking": "", "signature": ""}
                    if cb.get("type") == "tool_use":
                        has_tools = True
                        blocks[idx]["tool_idx"] = tool_idx; tool_idx += 1
                        yield emit({"tool_calls": [{"index": blocks[idx]["tool_idx"], "id": cb.get("id"), "type": "function",
                                                    "function": {"name": cb.get("name", ""), "arguments": ""}}]})
                    elif cb.get("type") == "server_tool_use" and cb.get("name") == "code_execution":
                        blocks[idx]["code"] = ""
                elif et == "content_block_delta":
                    idx = ev.get("index", 0); d = ev.get("delta") or {}; b = blocks.get(idx, {})
                    if d.get("type") == "text_delta":
                        yield emit({"content": d.get("text", "")})
                    elif d.get("type") == "thinking_delta":
                        b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                        yield emit({"reasoning_content": d.get("thinking", "")})
                    elif d.get("type") == "signature_delta":
                        b["signature"] = b.get("signature", "") + d.get("signature", "")
                    elif d.get("type") == "input_json_delta":
                        if b.get("type") == "tool_use":
                            yield emit({"tool_calls": [{"index": b.get("tool_idx", 0), "function": {"arguments": d.get("partial_json", "")}}]})
                        elif "code" in b:
                            b["code"] += d.get("partial_json", "")
                    elif d.get("type") == "citations_delta":
                        pass
                elif et == "content_block_stop":
                    idx = ev.get("index", 0); b = blocks.get(idx, {})
                    if b.get("type") == "tool_use" and b.get("id"):
                        tb = next((x for x in blocks.values() if x.get("type") == "thinking" and x.get("signature")), None)
                        if tb:
                            _THINKING_CACHE[b["id"]] = {"type": "thinking", "thinking": tb["thinking"], "signature": tb["signature"]}
                    if "code" in b:
                        try:
                            code = json.loads(b["code"]).get("code", "")
                        except Exception:
                            code = b["code"]
                        yield emit({"content": f"\n```python\n{code}\n```\n"})
                elif et == "message_delta":
                    stop_reason = (ev.get("delta") or {}).get("stop_reason")
                    usage_acc.update(ev.get("usage") or {})
                elif et == "message_stop":
                    break
                elif et == "error":
                    yield f"data: {json.dumps({'error': ev.get('error')})}\n\n"
                    break
        except Exception:
            _log_exc("stream")
        _record_usage(model, usage_acc)
        yield emit({}, _finish_reason(stop_reason, has_tools), _anthropic_usage_to_openai(usage_acc) if include_usage else None)
        yield "data: [DONE]\n\n"
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Admin ──────────────────────────────────────────────────────────────────
@app.route("/admin/accounts", methods=["GET"])
def admin_list():
    if err := _check_admin():
        return err
    out = accounts.list()
    if request.args.get("quota") in ("1", "true"):
        for e in out:
            a = accounts.get(e["api_key"])
            try:
                e["quota"] = fetch_quota(a) if a else None
            except Exception as ex:
                e["quota"] = {"error": str(ex)[:200]}
    return jsonify(out)


@app.route("/admin/accounts", methods=["POST"])
def admin_add():
    if err := _check_admin():
        return err
    b = request.get_json(force=True, silent=True) or {}
    key = (b.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "api_key is required"}), 400
    backend = b.get("backend", "anthropic")
    if backend == "anthropic" and not b.get("anthropic_key"):
        return jsonify({"error": "anthropic_key is required for backend=anthropic"}), 400
    a = accounts.add(key, label=b.get("label", ""), backend=backend, anthropic_key=b.get("anthropic_key", ""),
                     aws_region=b.get("aws_region", ""), aws_profile=b.get("aws_profile", ""))
    return jsonify(a.to_public()), 201


@app.route("/admin/accounts/<path:api_key>", methods=["DELETE"])
def admin_remove(api_key: str):
    if err := _check_admin():
        return err
    return (jsonify({"deleted": api_key}), 200) if accounts.remove(api_key) else (jsonify({"error": "not found"}), 404)


@app.route("/admin/accounts/<path:api_key>/quota")
def admin_quota(api_key: str):
    if err := _check_admin():
        return err
    a = accounts.get(api_key)
    if not a:
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(fetch_quota(a, refresh=request.args.get("refresh") in ("1", "true")))
    except Exception:
        return jsonify({"error": _log_exc("admin quota")}), 502


@app.route("/admin/accounts/<path:api_key>/test", methods=["POST"])
def admin_test(api_key: str):
    """Smoke test an account: one tiny request; returns latency, model and rate-limit headers."""
    if err := _check_admin():
        return err
    a = accounts.get(api_key)
    if not a:
        return jsonify({"error": "not found"}), 404
    model = (request.get_json(force=True, silent=True) or {}).get("model", "claude-haiku-4-5")
    t0 = time.time()
    try:
        r = upstream_messages(a, {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "reply with exactly: OK"}]}, [], False)
    except Exception:
        return jsonify({"ok": False, "error": _log_exc("admin test")}), 502
    ok = r.status_code == 200
    return jsonify({"ok": ok, "status": r.status_code, "latency_ms": int((time.time() - t0) * 1000),
                    "response": (r.json().get("content", [{}])[0].get("text") if ok else r.text[:300]),
                    "ratelimit": a.ratelimit})


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "64173")))
    args = p.parse_args()
    if not BRIDGE_ADMIN_KEY:
        log.warning("BRIDGE_ADMIN_KEY not set: /admin/* is OPEN. Set it unless bound to 127.0.0.1.")
    log.info("anthropic-bridge %s | .env: %d vars | debug=%s | upstream=%s | accounts=%d",
             BRIDGE_VERSION, _DOTENV_LOADED, BRIDGE_DEBUG, ANTHROPIC_BASE_URL, len(accounts._accounts))
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
