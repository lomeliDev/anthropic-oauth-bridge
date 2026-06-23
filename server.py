#!/usr/bin/env python3
"""
Anthropic OAuth -> OpenAI compatible bridge.

Run:
  pip install -r requirements.txt
  python3 server.py [--host 127.0.0.1] [--port 64173]

Environment variables:
  HOST / PORT               - listen address/port
  BRIDGE_API_KEY            - optional API key for client authentication
  ANTHROPIC_AUTH_PATH       - path to opencode auth.json
  CLAUDE_CREDENTIALS_PATH   - path to ~/.claude/.credentials.json
  ANTHROPIC_CLIENT_ID       - OAuth client id (default is the public Claude Code id)

Endpoints:
  GET  /health
  GET  /v1/models
  GET  /v1/models/<model_id>
  POST /v1/chat/completions      (stream + non-stream)

Compatible with OpenAI clients such as: Hermes, OpenClaw, Open WebUI, Continue, etc.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, jsonify, request

# ============================================================
# Config
# ============================================================
ANTHROPIC_AUTH_PATH = Path(os.environ.get(
    "ANTHROPIC_AUTH_PATH",
    Path.home() / ".local/share/opencode/auth.json",
))
CLAUDE_CREDENTIALS_PATH = Path(os.environ.get(
    "CLAUDE_CREDENTIALS_PATH",
    Path.home() / ".claude/.credentials.json",
))

BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY")

ANTHROPIC_CLIENT_ID = os.environ.get(
    "ANTHROPIC_CLIENT_ID",
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
OAUTH_TOKEN_URL = "https://claude.ai/v1/oauth/token"

FALLBACK_MODELS: list[dict[str, Any]] = [
    {"id": "claude-sonnet-4-5",       "object": "model", "owned_by": "anthropic", "created": 1735689600},
    {"id": "claude-opus-4-1",         "object": "model", "owned_by": "anthropic", "created": 1735776000},
    {"id": "claude-haiku-4-5",        "object": "model", "owned_by": "anthropic", "created": 1727654400},
]

# ============================================================
# Auth — loads credentials from auth.json / credentials.json
# ============================================================
class Auth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at: int = 0
        self._email: str | None = None
        self._subscription: str | None = None

    def _load(self) -> None:
        """Load access/refresh from the freshest available source."""
        # 1) opencode auth.json (preferred)
        if ANTHROPIC_AUTH_PATH.exists():
            try:
                data = json.loads(ANTHROPIC_AUTH_PATH.read_text())
                entry = data.get("anthropic") or {}
                if entry.get("access"):
                    self._access = entry["access"]
                if entry.get("refresh"):
                    self._refresh = entry["refresh"]
                if entry.get("expires"):
                    self._expires_at = int(entry["expires"])
            except Exception as e:
                print(f"[auth] warn reading auth.json: {e}", file=sys.stderr)

        # 2) Claude Code credentials.json (fallback / refresh source)
        if CLAUDE_CREDENTIALS_PATH.exists():
            try:
                data = json.loads(CLAUDE_CREDENTIALS_PATH.read_text())
                oauth = data.get("claudeAiOauth") or {}
                creds_expire = oauth.get("expiresAt", 0)
                # Only overwrite if credentials.json looks fresher or auth.json was empty.
                if oauth.get("accessToken") and (not self._access or creds_expire > self._expires_at):
                    self._access = oauth["accessToken"]
                    self._refresh = oauth.get("refreshToken") or self._refresh
                    self._expires_at = int(creds_expire)
                self._email = (oauth.get("account") or {}).get("email_address") or self._email
                self._subscription = oauth.get("subscriptionType") or self._subscription
            except Exception as e:
                print(f"[auth] warn reading credentials.json: {e}", file=sys.stderr)

    def _persist(self) -> None:
        """Write refreshed tokens back to both known locations."""
        if self._access and ANTHROPIC_AUTH_PATH.exists():
            try:
                ANTHROPIC_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                if ANTHROPIC_AUTH_PATH.exists():
                    data = json.loads(ANTHROPIC_AUTH_PATH.read_text())
                data.setdefault("anthropic", {})
                data["anthropic"]["type"] = "oauth"
                data["anthropic"]["access"] = self._access
                if self._refresh:
                    data["anthropic"]["refresh"] = self._refresh
                data["anthropic"]["expires"] = self._expires_at
                ANTHROPIC_AUTH_PATH.write_text(json.dumps(data, indent=2))
                ANTHROPIC_AUTH_PATH.chmod(0o600)
            except Exception as e:
                print(f"[auth] warn persisting auth.json: {e}", file=sys.stderr)

        if self._access and CLAUDE_CREDENTIALS_PATH.exists():
            try:
                CLAUDE_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
                data = json.loads(CLAUDE_CREDENTIALS_PATH.read_text())
                oauth = data.setdefault("claudeAiOauth", {})
                oauth["accessToken"] = self._access
                if self._refresh:
                    oauth["refreshToken"] = self._refresh
                oauth["expiresAt"] = self._expires_at
                CLAUDE_CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
                CLAUDE_CREDENTIALS_PATH.chmod(0o600)
            except Exception as e:
                print(f"[auth] warn persisting credentials.json: {e}", file=sys.stderr)

    def _refresh_token(self) -> None:
        if not self._refresh:
            raise RuntimeError("No refresh token available; run 'claude' and 'opencode auth login'")

        r = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": ANTHROPIC_CLIENT_ID,
                "refresh_token": self._refresh,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        r.raise_for_status()
        tok = r.json()
        if "access_token" not in tok:
            raise RuntimeError(f"Refresh response missing access_token: {tok}")
        self._access = tok["access_token"]
        # Anthropic rotates refresh tokens on every refresh.
        self._refresh = tok.get("refresh_token") or self._refresh
        self._expires_at = int(time.time() * 1000) + int(tok.get("expires_in", 28800)) * 1000
        self._email = (tok.get("account") or {}).get("email_address") or self._email
        self._persist()

    def get_token(self, allow_refresh: bool = True) -> str:
        with self._lock:
            self._load()
            now_ms = int(time.time() * 1000)
            if self._access and self._expires_at > now_ms + 30_000:
                return self._access
            if allow_refresh:
                self._refresh_token()
                return self._access
            raise RuntimeError("OAuth access token expired and refresh is disabled")

    @property
    def email(self) -> str | None:
        self._load()
        return self._email

    @property
    def subscription(self) -> str | None:
        self._load()
        return self._subscription


auth = Auth()
app = Flask(__name__)

# ============================================================
# Dynamic model list cache
# ============================================================
_MODEL_CACHE: list[dict[str, Any]] | None = None
_MODEL_CACHE_TS: float = 0.0
_MODEL_CACHE_TTL: float = 300.0


def _anthropic_headers(extra_beta: list[str] | None = None) -> dict[str, str]:
    # Base betas mirror the official opencode-claude-auth plugin as closely as possible.
    beta = [
        "claude-code-20250219",
        "oauth-2025-04-20",
        "prompt-caching-scope-2026-01-05",
        "context-management-2025-06-27",
        "advisor-tool-2026-03-01",
    ]
    if extra_beta:
        beta.extend(extra_beta)
    return {
        "Authorization": f"Bearer {auth.get_token()}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(beta),
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "user-agent": "claude-cli/2.1.112 (external, sdk-cli)",
        "x-client-request-id": str(uuid.uuid4()),
    }


def _anthropic_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    """Make an Anthropic API request with one automatic token refresh on 401."""
    url = f"{ANTHROPIC_BASE_URL}{path}"
    headers = kwargs.pop("headers", {})
    for attempt in (1, 2):
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if resp.status_code == 401 and attempt == 1:
            try:
                auth.get_token(allow_refresh=True)
                headers["Authorization"] = f"Bearer {auth.get_token()}"
                continue
            except Exception:
                break
        return resp
    return resp


def fetch_available_models() -> list[dict[str, Any]]:
    global _MODEL_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if _MODEL_CACHE is not None and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    try:
        r = _anthropic_request("GET", "/models", headers=_anthropic_headers())
        r.raise_for_status()
        data = r.json()
        models: list[dict[str, Any]] = []
        for m in data.get("data", []):
            if not m.get("id"):
                continue
            created = 1735689600
            if m.get("created_at"):
                try:
                    created = int(datetime.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass
            models.append({
                "id": m["id"],
                "object": "model",
                "owned_by": "anthropic",
                "created": created,
            })
        models.sort(key=lambda x: x["id"])
        _MODEL_CACHE = models
        _MODEL_CACHE_TS = now
        print(f"[models] fetched {len(models)} models", flush=True)
        return models
    except Exception as e:
        print(f"[models] fetch failed: {e}", file=sys.stderr, flush=True)
        if _MODEL_CACHE is not None:
            return _MODEL_CACHE
        return FALLBACK_MODELS


# ============================================================
# OpenAI -> Anthropic conversion helpers
# ============================================================
def _download_image(url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (mime_type, base64_data) for an image given by URL or data URI."""
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header.split(";")[0].replace("data:", "")
        return mime or "image/png", b64
    r = requests.get(url, headers={"User-Agent": "anthropic-oauth-bridge/0.1"}, timeout=timeout)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return mime, base64.b64encode(r.content).decode("ascii")


def _oai_content_to_anthropic(content: str | list[Any]) -> str | list[dict[str, Any]]:
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
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    })
                except Exception as e:
                    print(f"[vision] warn: {e}", file=sys.stderr, flush=True)
                    blocks.append({"type": "text", "text": "[image unavailable]"})
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
    suffix = "; ".join(parts)
    if original:
        return f"{original} ({suffix})"
    return suffix


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


def _oai_messages_to_anthropic(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]] | None]:
    tool_names = _tool_call_id_to_name(messages)
    system: str | list[dict[str, Any]] | None = None
    anthropic_messages: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                system = content
            elif isinstance(content, list):
                # Keep Anthropic native block array (supports cache_control).
                system = [{"type": "text", "text": " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )}]
            continue

        if role == "user":
            anthropic_messages.append({"role": "user", "content": _oai_content_to_anthropic(content)})
        elif role == "assistant":
            if isinstance(content, list):
                # Convert OpenAI tool_calls back to Anthropic tool_use blocks.
                blocks: list[dict[str, Any]] = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        blocks.append({"type": "text", "text": item.get("text", "")})
                    elif item.get("type") == "tool_calls":
                        # Not standard OpenAI; tolerate inline tool_calls arrays.
                        for tc in item.get("tool_calls", []):
                            if isinstance(tc, dict):
                                blocks.append({
                                    "type": "tool_use",
                                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                                    "name": tc.get("function", {}).get("name", "unknown"),
                                    "input": json.loads(tc.get("function", {}).get("arguments", "{}")) or {},
                                })
                if not blocks:
                    blocks = [{"type": "text", "text": " "}]
                anthropic_messages.append({"role": "assistant", "content": blocks})
            elif isinstance(content, str):
                anthropic_messages.append({"role": "assistant", "content": content or " "})
        elif role in ("tool", "function"):
            tid = msg.get("tool_call_id", "")
            name = msg.get("name") or tool_names.get(tid) or "unknown"
            tool_content = content
            if isinstance(content, (dict, list)):
                tool_content = json.dumps(content)
            anthropic_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": tool_content or " ",
                }],
            })

    return anthropic_messages, system


def _build_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    model = body.get("model", "claude-sonnet-4-5")
    messages = body.get("messages", [])
    anthropic_messages, system = _oai_messages_to_anthropic(messages)

    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    if max_tokens is None:
        max_tokens = 4096

    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": anthropic_messages,
    }
    if system:
        req["system"] = system

    if "temperature" in body:
        req["temperature"] = body["temperature"]
    if "top_p" in body:
        req["top_p"] = body["top_p"]
    if "top_k" in body:
        req["top_k"] = body["top_k"]
    stop = body.get("stop")
    if stop:
        req["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if "metadata" in body:
        req["metadata"] = body["metadata"]

    # Tools / tool_choice
    tools = body.get("tools") or body.get("functions")
    anthropic_tools = _oai_tools_to_anthropic(tools or [])
    if anthropic_tools:
        req["tools"] = anthropic_tools
        tc = _oai_tool_choice_to_anthropic(body.get("tool_choice"))
        if tc:
            req["tool_choice"] = tc

    # Structured output via forced tool.
    rf = body.get("response_format")
    if isinstance(rf, dict):
        rf_type = rf.get("type")
        if rf_type == "json_object":
            schema = rf.get("json_schema", {}).get("schema") or {"type": "object"}
            req.setdefault("tools", []).append({
                "name": "json_object_response",
                "description": "Respond with a JSON object.",
                "input_schema": _sanitize_schema_for_claude(schema),
            })
            req["tool_choice"] = {"type": "tool", "name": "json_object_response"}
        elif rf_type == "json_schema":
            schema = rf.get("json_schema", {}).get("schema") or {"type": "object"}
            req.setdefault("tools", []).append({
                "name": "json_schema_response",
                "description": "Respond matching the requested JSON schema.",
                "input_schema": _sanitize_schema_for_claude(schema),
            })
            req["tool_choice"] = {"type": "tool", "name": "json_schema_response"}

    if "thinking" in body and isinstance(body["thinking"], dict):
        req["thinking"] = body["thinking"]

    return req


# ============================================================
# Anthropic -> OpenAI conversion helpers
# ============================================================
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
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
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
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }


# ============================================================
# API key authentication
# ============================================================
def _check_api_key() -> tuple[dict[str, Any], int] | None:
    if not BRIDGE_API_KEY:
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"error": {"message": "Missing Authorization header", "type": "authentication_error"}}, 401
    if auth_header[7:] != BRIDGE_API_KEY:
        return {"error": {"message": "Invalid API key", "type": "authentication_error"}}, 401
    return None


# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    return jsonify({
        "name": "anthropic-oauth-bridge",
        "version": "0.1.0",
        "openai_compatible": True,
        "email": auth.email,
        "upstream": ANTHROPIC_BASE_URL,
        "endpoints": ["/health", "/v1/models", "/v1/chat/completions"],
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "email": auth.email,
        "subscription": auth.subscription,
        "token_expires_at": auth._expires_at,
        "now_ms": int(time.time() * 1000),
    })


@app.route("/v1/models")
def list_models():
    if err := _check_api_key():
        return jsonify(err[0]), err[1]
    return jsonify({"object": "list", "data": fetch_available_models()})


@app.route("/v1/models/<path:model_id>")
def get_model(model_id: str):
    if err := _check_api_key():
        return jsonify(err[0]), err[1]
    for m in fetch_available_models():
        if m["id"] == model_id:
            return jsonify(m)
    return jsonify({"error": {"message": "model not found", "type": "invalid_request_error"}}), 404


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if err := _check_api_key():
        return jsonify(err[0]), err[1]

    body = request.get_json(force=True, silent=True) or {}
    if not body.get("messages"):
        return jsonify({"error": {"message": "messages is required", "type": "invalid_request_error"}}), 400

    anthropic_req = _build_anthropic_request(body)
    model = body.get("model", "claude-sonnet-4-5")
    stream = bool(body.get("stream", False))
    stream_options = body.get("stream_options") or {}
    include_usage = bool(stream_options.get("include_usage"))

    extra_beta: list[str] = []
    if "thinking" in body and isinstance(body["thinking"], dict):
        extra_beta.append("interleaved-thinking-2025-05-14")

    headers = _anthropic_headers(extra_beta=extra_beta)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if stream:
        def gen():
            try:
                resp = _anthropic_request(
                    "POST",
                    "/messages?beta=true",
                    headers=headers,
                    json=anthropic_req,
                    stream=True,
                )
                if resp.status_code != 200:
                    err = {"error": {"message": resp.text, "type": "upstream_error", "code": resp.status_code}}
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                current_text = ""
                current_tool: dict[str, Any] | None = None
                tool_index = 0
                message_usage: dict[str, Any] = {}

                for line in resp.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8")
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    try:
                        ev = json.loads(decoded)
                    except Exception:
                        continue

                    etype = ev.get("type")
                    if etype == "message_start":
                        msg = ev.get("message") or {}
                        if include_usage:
                            message_usage = msg.get("usage") or {}
                    elif etype == "content_block_start":
                        block = ev.get("content_block") or {}
                        btype = block.get("type")
                        if btype == "text":
                            current_text = ""
                        elif btype == "tool_use":
                            current_tool = {
                                "index": tool_index,
                                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                "type": "function",
                                "function": {"name": block.get("name", ""), "arguments": ""},
                            }
                    elif etype == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta" and "text" in delta:
                            chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": delta["text"]},
                                    "finish_reason": None,
                                }],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif delta.get("type") == "input_json_delta" and current_tool:
                            current_tool["function"]["arguments"] += delta.get("partial_json", "")
                        elif delta.get("type") == "thinking_delta":
                            # Surface thinking as an internal field; most clients ignore it.
                            chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": f"[thinking: {delta.get('thinking', '')}]"},
                                    "finish_reason": None,
                                }],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                    elif etype == "content_block_stop":
                        if current_tool:
                            chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [{
                                            "index": tool_index,
                                            "id": current_tool["id"],
                                            "type": "function",
                                            "function": {
                                                "name": current_tool["function"]["name"],
                                                "arguments": current_tool["function"]["arguments"],
                                            },
                                        }],
                                    },
                                    "finish_reason": None,
                                }],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                            current_tool = None
                            tool_index += 1
                    elif etype == "message_delta":
                        d = ev.get("delta") or {}
                        if d.get("usage"):
                            message_usage = d["usage"]

                final: dict[str, Any] = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                if include_usage:
                    final["usage"] = _anthropic_usage_to_openai(message_usage)
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err = {"error": {"message": str(e), "type": "server_error"}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-stream
    try:
        resp = _anthropic_request("POST", "/messages?beta=true", headers=headers, json=anthropic_req)
    except Exception as e:
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502

    if resp.status_code != 200:
        return jsonify({"error": {"message": resp.text, "type": "upstream_error", "code": resp.status_code}}), resp.status_code

    data = resp.json()
    content = data.get("content", [])
    text, tool_calls, _thinking = _anthropic_content_to_openai_message(content)

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = data.get("stop_reason", "stop")
    if finish_reason == "tool_use":
        finish_reason = "tool_calls"

    return jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _anthropic_usage_to_openai(data.get("usage") or {}),
    })


# ============================================================
# Main
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "64173")))
    args = parser.parse_args()

    try:
        tok = auth.get_token()
        print(f"[bridge] email      : {auth.email}", flush=True)
        print(f"[bridge] token      : {tok[:12]}...", flush=True)
        print(f"[bridge] token exp  : {auth._expires_at}", flush=True)
    except Exception as e:
        print(f"[bridge] WARN init: {e}", flush=True)

    print(f"[bridge] listening  : http://{args.host}:{args.port}", flush=True)
    try:
        models = fetch_available_models()
        print(f"[bridge] models     : {len(models)} available", flush=True)
    except Exception as e:
        print(f"[bridge] models     : static fallback ({e})", flush=True)

    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
