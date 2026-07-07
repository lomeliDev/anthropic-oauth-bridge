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
import hashlib
import json
import os
import re
import secrets
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
# Anthropic migrated to platform.claude.com; console.anthropic.com now 404s.
OAUTH_TOKEN_URLS = [
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]

# Claude Code version for billing header (matches OpenCode plugin).
CLAUDE_CODE_VERSION = os.environ.get("ANTHROPIC_CLI_VERSION", "2.1.112")
CLAUDE_CODE_ENTRYPOINT = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")

# ── Claude model config (mirrors Hermes anthropic_adapter + OpenCode) ──

# Common betas for ALL Anthropic requests (matches Hermes _COMMON_BETAS)
CLAUDE_COMMON_BETAS = [
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
]

# OAuth-only betas (matches Hermes _OAUTH_ONLY_BETAS)
CLAUDE_OAUTH_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
]

# Full OAuth beta set = common + OAuth-only
CLAUDE_OAUTH_ALL_BETAS = CLAUDE_COMMON_BETAS + CLAUDE_OAUTH_BETAS

# Additional betas for specific features
CLAUDE_LONG_CONTEXT_BETA = "context-1m-2025-08-07"
CLAUDE_FAST_MODE_BETA = "fast-mode-2026-02-01"
CLAUDE_EFFORT_BETA = "effort-2025-11-24"

# Long context betas (for auto-exclusion recovery)
CLAUDE_LONG_CONTEXT_BETAS = [
    CLAUDE_LONG_CONTEXT_BETA,
    "interleaved-thinking-2025-05-14",
]

# Model-specific overrides
CLAUDE_MODEL_OVERRIDES: dict[str, dict[str, Any]] = {
    "haiku": {
        "exclude": ["interleaved-thinking-2025-05-14"],
        "disable_effort": True,
        "disable_thinking": True,
    },
    "4-6": {
        "add": [CLAUDE_EFFORT_BETA],
        "supports_fast_mode": True,
    },
    "4-7": {
        "add": [CLAUDE_EFFORT_BETA],
        "forbids_sampling_params": True,
    },
}

# Runtime beta exclusion tracking (auto-recovery from errors)
_excluded_betas: dict[str, set[str]] = {}

def _get_model_betas(model_id: str, excluded: set[str] | None = None, is_oauth: bool = True) -> list[str]:
    """Get beta headers for a model, applying overrides and exclusions."""
    # Start with OAuth betas or common-only depending on auth mode
    betas = list(CLAUDE_OAUTH_ALL_BETAS if is_oauth else CLAUDE_COMMON_BETAS)

    # Apply model overrides
    lower = model_id.lower()
    for pattern, override in CLAUDE_MODEL_OVERRIDES.items():
        if pattern in lower:
            if "exclude" in override:
                for ex in override["exclude"]:
                    if ex in betas:
                        betas.remove(ex)
            if "add" in override:
                for add_beta in override["add"]:
                    if add_beta not in betas:
                        betas.append(add_beta)
            if override.get("supports_fast_mode") and is_oauth:
                if CLAUDE_FAST_MODE_BETA not in betas:
                    betas.append(CLAUDE_FAST_MODE_BETA)
            if override.get("forbids_sampling_params"):
                _current_model_forbids_sampling = True
            break  # First match wins

    # Filter excluded betas
    if excluded:
        betas = [b for b in betas if b not in excluded]

    return betas


def _get_model_override(model_id: str) -> dict[str, Any] | None:
    """Find model override entry by substring match."""
    lower = model_id.lower()
    for pattern, override in CLAUDE_MODEL_OVERRIDES.items():
        if pattern in lower:
            return override
    return None


def _supports_fast_mode(model_id: str) -> bool:
    """Check if model supports fast mode (Opus 4.6 only)."""
    override = _get_model_override(model_id)
    return bool(override and override.get("supports_fast_mode"))


def _forbids_sampling_params(model_id: str) -> bool:
    """Check if model rejects temperature/top_p/top_k (Opus 4.7+)."""
    override = _get_model_override(model_id)
    return bool(override and override.get("forbids_sampling_params"))


def _is_long_context_error(response_body: str) -> bool:
    """Detect Anthropic 'extra usage required for long context' errors."""
    return (
        "Extra usage is required for long context requests" in response_body
        or "long context beta is not yet available" in response_body
        or "You're out of extra usage" in response_body
    )


def _get_next_beta_to_exclude(model_id: str) -> str | None:
    """Find next long-context beta to exclude for auto-recovery."""
    excluded = _excluded_betas.get(model_id, set())
    for beta in CLAUDE_LONG_CONTEXT_BETAS:
        if beta not in excluded:
            return beta
    return None


def _add_excluded_beta(model_id: str, beta: str) -> None:
    """Mark a beta as excluded for a model (auto-recovery)."""
    if model_id not in _excluded_betas:
        _excluded_betas[model_id] = set()
    _excluded_betas[model_id].add(beta)


def _supports_effort(model_id: str) -> bool:
    """Check if model supports the effort parameter."""
    override = _get_model_override(model_id)
    return not (override and override.get("disable_effort"))

FALLBACK_MODELS: list[dict[str, Any]] = [
    {"id": "claude-sonnet-4-5",       "object": "model", "owned_by": "anthropic", "created": 1735689600},
    {"id": "claude-opus-4-1",         "object": "model", "owned_by": "anthropic", "created": 1735776000},
    {"id": "claude-haiku-4-5",        "object": "model", "owned_by": "anthropic", "created": 1727654400},
]

# ============================================================
# Multi-account / credential pool support
# ============================================================
# accounts.json maps API keys to Anthropic OAuth accounts:
# {
#   "accounts": {
#     "sk-xxx": {"label": "Account 1", "refresh_token": "...", "client_id": "..."}
#   }
# }
BRIDGE_ACCOUNTS_FILE = Path(os.environ.get(
    "BRIDGE_ACCOUNTS_FILE",
    str(Path(__file__).resolve().parent / "accounts.json"),
))
BRIDGE_ADMIN_KEY = os.environ.get("BRIDGE_ADMIN_KEY", "")

# Credential pool: per-account cached access tokens
AUTH_CACHE_DIR = Path(os.environ.get(
    "BRIDGE_AUTH_CACHE_DIR",
    str(Path(__file__).resolve().parent / "auth_cache"),
))

# ============================================================
# Auth — multi-account credential pool with race-safe refresh
# ============================================================

class Auth:
    """Multi-account Anthropic OAuth credential manager.

    Sources (in priority order, matching Hermes' resolve_anthropic_token):
      1. auth.json (OpenCode-synced)
      2. ~/.claude/.credentials.json (Claude Code native)
      3. accounts.json (multi-account via admin API)
      4. BRIDGE_REFRESH_TOKEN env var (single-account legacy)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at: int = 0
        self._email: str | None = None
        self._subscription: str | None = None
        # Multi-account state
        self._accounts: dict[str, dict[str, Any]] = {}  # api_key -> account dict
        self._active_api_key: str = ""
        self._account_lock = threading.Lock()

    # ── Source loading ──────────────────────────────────────

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

    # ── Race-safe re-read (Hermes pattern) ─────────────────

    def _reread_credentials(self) -> dict[str, Any] | None:
        """Re-read Claude Code credential files WITHOUT mutating state.

        Used before refresh to detect if Claude Code already rotated the token.
        Returns {accessToken, refreshToken, expiresAt} or None.
        """
        # Check auth.json first
        if ANTHROPIC_AUTH_PATH.exists():
            try:
                data = json.loads(ANTHROPIC_AUTH_PATH.read_text())
                entry = data.get("anthropic") or {}
                if entry.get("access") and entry.get("expires", 0) > int(time.time() * 1000) + 30_000:
                    return {
                        "accessToken": entry["access"],
                        "refreshToken": entry.get("refresh", ""),
                        "expiresAt": int(entry["expires"]),
                    }
            except Exception:
                pass
        # Check credentials.json
        if CLAUDE_CREDENTIALS_PATH.exists():
            try:
                data = json.loads(CLAUDE_CREDENTIALS_PATH.read_text())
                oauth = data.get("claudeAiOauth") or {}
                if oauth.get("accessToken") and oauth.get("expiresAt", 0) > int(time.time() * 1000) + 30_000:
                    return {
                        "accessToken": oauth["accessToken"],
                        "refreshToken": oauth.get("refreshToken", ""),
                        "expiresAt": int(oauth["expiresAt"]),
                    }
            except Exception:
                pass
        return None

    # ── Persist ─────────────────────────────────────────────

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
                tmp = ANTHROPIC_AUTH_PATH.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
                try:
                    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, indent=2)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, ANTHROPIC_AUTH_PATH)
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
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
                tmp = CLAUDE_CREDENTIALS_PATH.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
                try:
                    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, indent=2)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, CLAUDE_CREDENTIALS_PATH)
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception as e:
                print(f"[auth] warn persisting credentials.json: {e}", file=sys.stderr)

    # ── Token refresh ───────────────────────────────────────

    def _refresh_token(self) -> None:
        if not self._refresh:
            raise RuntimeError("No refresh token available; run 'claude' and 'opencode auth login'")

        now_ms = int(time.time() * 1000)

        # Race-safe: re-read credentials first to avoid racing Claude Code
        fresh = self._reread_credentials()
        if fresh and fresh["accessToken"] != self._access and fresh["expiresAt"] > now_ms + 30_000:
            print("[auth] adopted Claude Code's already-refreshed token", flush=True)
            self._access = fresh["accessToken"]
            self._refresh = fresh.get("refreshToken") or self._refresh
            self._expires_at = fresh["expiresAt"]
            self._persist()
            return

        refresh_token = (fresh or {}).get("refreshToken") or self._refresh
        if not refresh_token:
            raise RuntimeError("No refresh token available")

        # Try multiple token endpoints (platform.claude.com first, then console.anthropic.com)
        last_error = None
        for endpoint in OAUTH_TOKEN_URLS:
            try:
                r = requests.post(
                    endpoint,
                    data={
                        "client_id": ANTHROPIC_CLIENT_ID,
                        "refresh_token": refresh_token,
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
                self._expires_at = now_ms + int(tok.get("expires_in", 28800)) * 1000
                self._email = (tok.get("account") or {}).get("email_address") or self._email
                self._persist()
                return
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(f"Token refresh failed at all endpoints: {last_error}")

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

    # ── Multi-account support ───────────────────────────────

    def _load_accounts(self) -> None:
        """Load multi-account config from accounts.json."""
        if not BRIDGE_ACCOUNTS_FILE.exists():
            return
        try:
            with self._account_lock:
                data = json.loads(BRIDGE_ACCOUNTS_FILE.read_text())
                self._accounts = data.get("accounts", {})
        except Exception as e:
            print(f"[auth] warn loading accounts.json: {e}", file=sys.stderr)

    def _save_accounts(self) -> None:
        """Persist accounts back to accounts.json."""
        BRIDGE_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BRIDGE_ACCOUNTS_FILE.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"accounts": self._accounts}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, BRIDGE_ACCOUNTS_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def resolve_account_by_key(self, api_key: str) -> dict[str, Any] | None:
        """Find account by API key. Returns account dict or None."""
        self._load_accounts()
        return self._accounts.get(api_key)

    def get_token_for_account(self, api_key: str) -> str:
        """Get a fresh access token for a specific multi-account entry."""
        account = self.resolve_account_by_key(api_key)
        if not account:
            raise RuntimeError(f"Unknown API key: {api_key[:16]}...")

        refresh_token = account.get("refresh_token", "")
        if not refresh_token:
            raise RuntimeError(f"No refresh token for account: {account.get('label', 'unknown')}")

        # Check cached token
        cache_file = AUTH_CACHE_DIR / f"{hashlib.sha256(api_key.encode()).hexdigest()[:16]}.json"
        now_ms = int(time.time() * 1000)

        AUTH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(AUTH_CACHE_DIR), 0o700)
        except OSError:
            pass

        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                if cached.get("expires_at", 0) > now_ms + 30_000:
                    return cached["access_token"]
            except Exception:
                pass

        # Refresh
        client_id = account.get("client_id", ANTHROPIC_CLIENT_ID)
        last_error = None
        for endpoint in OAUTH_TOKEN_URLS:
            try:
                r = requests.post(
                    endpoint,
                    data={
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                    timeout=20,
                )
                r.raise_for_status()
                tok = r.json()
                if "access_token" not in tok:
                    raise RuntimeError(f"Missing access_token")
                access = tok["access_token"]
                new_refresh = tok.get("refresh_token", refresh_token)
                new_expires = now_ms + int(tok.get("expires_in", 28800)) * 1000

                # Save to cache
                try:
                    tmp_cache = cache_file.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
                    fd = os.open(str(tmp_cache), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump({
                            "access_token": access,
                            "refresh_token": new_refresh,
                            "expires_at": new_expires,
                        }, fh)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp_cache, cache_file)
                finally:
                    try:
                        tmp_cache.unlink(missing_ok=True)
                    except OSError:
                        pass

                # Update accounts.json if refresh_token changed
                if new_refresh != refresh_token:
                    account["refresh_token"] = new_refresh
                    self._save_accounts()

                return access
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(f"Account token refresh failed: {last_error}")

    def list_accounts(self) -> list[dict[str, Any]]:
        """List all accounts (without secrets)."""
        self._load_accounts()
        result = []
        for api_key, account in self._accounts.items():
            result.append({
                "api_key_prefix": api_key[:16] + "...",
                "label": account.get("label", ""),
                "email": account.get("email", ""),
                "has_refresh_token": bool(account.get("refresh_token")),
            })
        return result

    def add_account(self, api_key: str, label: str, refresh_token: str,
                    client_id: str = "", email: str = "") -> dict[str, Any]:
        """Add a new multi-account entry."""
        self._load_accounts()
        self._accounts[api_key] = {
            "label": label,
            "refresh_token": refresh_token,
            "client_id": client_id or ANTHROPIC_CLIENT_ID,
            "email": email,
        }
        self._save_accounts()
        return {"ok": True, "api_key_prefix": api_key[:16] + "..."}

    def remove_account(self, api_key: str) -> dict[str, Any]:
        """Remove a multi-account entry."""
        self._load_accounts()
        if api_key in self._accounts:
            del self._accounts[api_key]
            self._save_accounts()
            # Clear cache
            cache_file = AUTH_CACHE_DIR / f"{hashlib.sha256(api_key.encode()).hexdigest()[:16]}.json"
            try:
                cache_file.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": True}
        return {"ok": False, "error": "Account not found"}

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


def _anthropic_headers(extra_beta: list[str] | None = None, model_id: str = "unknown") -> dict[str, str]:
    # Dynamic betas per model, excluding previously-failed betas
    excluded = _excluded_betas.get(model_id, set())
    model_betas = _get_model_betas(model_id, excluded=excluded)
    if extra_beta:
        for b in extra_beta:
            if b not in model_betas:
                model_betas.append(b)

    token = _get_access_token_for_request()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(model_betas),
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        # Anthropic BLOCKS "claude-cli/" on the token endpoint — must use "claude-code/"
        "user-agent": f"claude-code/{CLAUDE_CODE_VERSION} (external, cli)",
        "x-client-request-id": str(uuid.uuid4()),
        # Stainless headers (mirrors Claude Code SDK fingerprint)
        "x-stainless-arch": "arm64" if "aarch64" in os.uname().machine else os.uname().machine,
        "x-stainless-lang": "js",
        "x-stainless-os": "MacOS" if sys.platform == "darwin" else sys.platform,
        "x-stainless-package-version": "0.81.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-timeout": "600",
    }


def _anthropic_request(method: str, path: str, *, model_id: str = "unknown", **kwargs: Any) -> requests.Response:
    """Make an Anthropic API request with automatic retry and beta recovery.

    Retry ladder:
      1. 401 → refresh token + retry once
      2. 429/529 → exponential backoff with 30s cap
      3. 400/429 + long-context error → exclude beta + retry
    """
    url = f"{ANTHROPIC_BASE_URL}{path}"
    headers = kwargs.pop("headers", {})
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)

        # 401 — token refresh + retry once
        if resp.status_code == 401 and attempt == 1:
            try:
                fresh_token = auth.get_token(allow_refresh=True)
                headers["Authorization"] = f"Bearer {fresh_token}"
                continue
            except Exception:
                break

        # 429/529 — rate limit with exponential backoff
        if resp.status_code in (429, 529) and attempt < max_retries:
            retry_after = resp.headers.get("retry-after")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else attempt * 2
            # Cap at 30s — longer means quota reset, don't wait
            if delay > 30:
                print(f"[upstream] rate limited (quota reset in {delay}s) — returning error", file=sys.stderr)
                return resp
            print(f"[upstream] rate limited — retrying in {delay}s (attempt {attempt}/{max_retries})", file=sys.stderr)
            time.sleep(delay)
            continue

        # 400/429 + long-context error — exclude problematic beta + retry
        if resp.status_code in (400, 429) and attempt < max_retries:
            try:
                body = resp.text
            except Exception:
                body = ""
            if _is_long_context_error(body):
                beta_to_exclude = _get_next_beta_to_exclude(model_id)
                if beta_to_exclude:
                    _add_excluded_beta(model_id, beta_to_exclude)
                    print(f"[upstream] excluding beta '{beta_to_exclude}' for {model_id} — retrying", file=sys.stderr)
                    # Rebuild headers with updated betas
                    new_headers = _anthropic_headers(model_id=model_id)
                    headers = new_headers
                    continue

        return resp
    return resp


def fetch_available_models() -> list[dict[str, Any]]:
    global _MODEL_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if _MODEL_CACHE is not None and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    try:
        r = _anthropic_request("GET", "/models", headers=_anthropic_headers(model_id="models"))
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

# ── Claude Code request transforms ──────────────────────────
# These mirrors the opencode-claude-auth plugin's transforms.js.
# Without these, Anthropic's OAuth validation rejects requests
# with 400 errors ("out of extra usage", "invalid tool name", etc.)

SYSTEM_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
BILLING_SALT = "59cf53e54c78"
TOOL_PREFIX = "mcp_"

def _extract_first_user_message_text(messages: list[dict[str, Any]]) -> str:
    """Extract text from the first user message's first text block."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""

def _compute_cch(message_text: str) -> str:
    """First 5 hex chars of SHA-256(messageText)."""
    return hashlib.sha256(message_text.encode()).hexdigest()[:5]

def _compute_version_suffix(message_text: str, version: str) -> str:
    """3-char hash suffix for billing header."""
    sampled = "".join(message_text[i] if i < len(message_text) else "0" for i in (4, 7, 20))
    inp = f"{BILLING_SALT}{sampled}{version}"
    return hashlib.sha256(inp.encode()).hexdigest()[:3]

def _build_billing_header(messages: list[dict[str, Any]]) -> str:
    """Build x-anthropic-billing-header string.

    Format: cc_version=V.S; cc_entrypoint=E; cch=H;
    """
    text = _extract_first_user_message_text(messages)
    suffix = _compute_version_suffix(text, CLAUDE_CODE_VERSION)
    cch = _compute_cch(text)
    return (
        f"x-anthropic-billing-header: "
        f"cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
        f"cc_entrypoint={CLAUDE_CODE_ENTRYPOINT}; "
        f"cch={cch};"
    )

def _pascal_case_tool_name(name: str) -> str:
    """ALL tools get mcp__ prefix for OAuth (Hermes pattern).

    Hermes discovered that on OAuth, ALL tool names must use mcp__ prefix.
    Bare tools (read_file) → mcp__Read_file
    Single-underscore MCP tools (mcp_server_tool) → mcp__Server_tool
    Already double-underscore → keep as-is
    Non-OAuth mode (api_key) → keep original name
    """
    if not name:
        return name
    if name.startswith("mcp__"):
        return name  # already correct
    if name.startswith("mcp_"):
        return "mcp__" + name[4:]  # single → double underscore
    return "mcp__" + name  # bare → mcp__ prefix + keep original case


def _unprefix_tool_name(name: str) -> str:
    """Reverse pascal_case: mcp__Bash → mcp_bash."""
    if name.startswith("mcp__") and len(name) > 5:
        return f"mcp_{name[5].lower()}{name[6:]}"
    return name
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

def _transform_anthropic_request(req: dict[str, Any]) -> dict[str, Any]:
    """Apply Claude Code OAuth transforms to the Anthropic request body.

    1. Billing header → system[0] (no cache_control)
    2. System identity → must be a standalone system entry
    3. Non-billing/identity system → moved to first user message
    4. Tool names → PascalCase (mcp_Bash not mcp_bash)
    5. Orphan tool repair → remove mismatched tool_use/tool_result
    """
    messages = req.get("messages", [])

    # 1. Billing header
    billing_header = _build_billing_header(messages)
    system = req.get("system")
    if not isinstance(system, list):
        system = [{"type": "text", "text": system}] if isinstance(system, str) else []

    # Remove any existing billing header entries
    system = [
        e for e in system
        if not (isinstance(e, dict) and e.get("type") == "text"
                and isinstance(e.get("text"), str)
                and e["text"].startswith("x-anthropic-billing-header"))
    ]

    # 2. Split system identity from other system content
    BILLING_PREFIX = "x-anthropic-billing-header"
    kept_system: list[dict[str, Any]] = []
    moved_texts: list[str] = []

    for entry in system:
        txt = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        if isinstance(entry, dict):
            if txt.startswith(BILLING_PREFIX) or txt.startswith(SYSTEM_IDENTITY):
                kept_system.append(entry)
            elif txt.strip():
                moved_texts.append(txt)
        elif isinstance(entry, str) and entry.strip():
            if entry.startswith(BILLING_PREFIX) or entry.startswith(SYSTEM_IDENTITY):
                kept_system.append({"type": "text", "text": entry})
            else:
                moved_texts.append(entry)

    # Insert billing header as system[0]
    kept_system.insert(0, {"type": "text", "text": billing_header})

    # 3. Relocate non-identity system content to first user message
    if moved_texts:
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        if first_user:
            prefix = "\n\n".join(moved_texts)
            content = first_user.get("content", "")
            if isinstance(content, str):
                first_user["content"] = f"{prefix}\n\n{content}"
            elif isinstance(content, list):
                content.insert(0, {"type": "text", "text": prefix})

    req["system"] = kept_system

    # 3.5 System prompt sanitization (Hermes pattern)
    # Replace product name references to avoid Anthropic content filters
    for entry in kept_system:
        if isinstance(entry, dict) and entry.get("type") == "text":
            text = entry.get("text", "")
            text = text.replace("Hermes Agent", "Claude Code")
            text = text.replace("Hermes agent", "Claude Code")
            text = text.replace("hermes-agent", "claude-code")
            text = text.replace("Nous Research", "Anthropic")
            entry["text"] = text

    # 4. Tool PascalCase
    if isinstance(req.get("tools"), list):
        for tool in req["tools"]:
            if isinstance(tool, dict) and tool.get("name"):
                tool["name"] = _pascal_case_tool_name(tool["name"])

    # Also transform tool names in conversation history
    for msg in messages:
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                block["name"] = _pascal_case_tool_name(block["name"])

    # 5. Orphan tool repair
    req["messages"] = _repair_orphan_tool_pairs(messages)

    return req

def _strip_tool_prefix_from_response(text: str) -> str:
    """Reverse tool name prefixing in response text."""
    import re as _re
    return _re.sub(
        r'"name"\s*:\s*"mcp_([^"]+)"',
        lambda m: f'"name": "{_unprefix_tool_name("mcp_" + m.group(1))}"',
        text,
    )


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
        override = _get_model_override(model)
        if not (override and override.get("disable_thinking")):
            req["thinking"] = body["thinking"]

    # ── Fast mode (Opus 4.6 only) ─────────────────────────────
    # Adds speed=fast for ~2.5x output throughput.
    if body.get("fast_mode") and _supports_fast_mode(model):
        req.setdefault("extra_body", {})["speed"] = "fast"

    # ── Strip sampling params on 4.7+ ─────────────────────────
    # Opus 4.7+ rejects non-default temperature/top_p/top_k
    if _forbids_sampling_params(model):
        for key in ("temperature", "top_p", "top_k"):
            req.pop(key, None)

    # Apply Claude Code OAuth transforms for billing/system identity/tool naming
    req = _transform_anthropic_request(req)

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
            raw_name = block.get("name", "")
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": _unprefix_tool_name(raw_name),
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
def _check_api_key() -> tuple[dict[str, Any], int] | None:
    """Validate client API key against BRIDGE_API_KEY or accounts.json.

    Returns None if auth passes, or (error_dict, status_code) if not.
    When multi-account is enabled (accounts.json has entries), any valid
    account API key is accepted. Otherwise, falls back to BRIDGE_API_KEY.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # No auth header — allow if bridge has no auth configured
        if not BRIDGE_API_KEY and not auth._accounts:
            return None
        return {"error": {"message": "Missing Authorization header", "type": "authentication_error"}}, 401

    api_key = auth_header[7:]

    # Multi-account: check accounts.json
    if api_key in auth._accounts:
        return None

    # Single-account: check BRIDGE_API_KEY
    if BRIDGE_API_KEY and api_key == BRIDGE_API_KEY:
        return None

    return {"error": {"message": "Invalid API key", "type": "authentication_error"}}, 401


def _get_access_token_for_request() -> str:
    """Resolve access token: multi-account API key → account token, else default."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        if api_key in auth._accounts:
            return auth.get_token_for_account(api_key)
    return auth.get_token()


def _admin_auth() -> tuple[dict[str, Any], int] | None:
    """Authenticate admin API requests."""
    if not BRIDGE_ADMIN_KEY:
        return None  # Admin API open (dev mode)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"error": {"message": "Missing Authorization header", "type": "authentication_error"}}, 401
    if auth_header[7:] != BRIDGE_ADMIN_KEY:
        return {"error": {"message": "Invalid admin key", "type": "authentication_error"}}, 401
    return None


# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    return jsonify({
        "name": "anthropic-oauth-bridge",
        "version": "0.2.0",
        "openai_compatible": True,
        "email": auth.email,
        "upstream": ANTHROPIC_BASE_URL,
        "endpoints": ["/health", "/v1/models", "/v1/chat/completions", "/v1/usage"],
        "admin_endpoints": ["/admin/accounts", "/admin/health"],
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "email": auth.email,
        "subscription": auth.subscription,
        "token_expires_at": auth._expires_at,
        "now_ms": int(time.time() * 1000),
        "multi_account_enabled": bool(auth._accounts),
        "account_count": len(auth._accounts),
    })


# ── Admin endpoints ─────────────────────────────────────────

@app.route("/admin/health")
def admin_health():
    """Full health check including credential status."""
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    return jsonify({
        "status": "ok",
        "email": auth.email,
        "subscription": auth.subscription,
        "token_expires_at": auth._expires_at,
        "now_ms": int(time.time() * 1000),
        "accounts": auth.list_accounts(),
        "token_endpoints": OAUTH_TOKEN_URLS,
    })


@app.route("/admin/accounts", methods=["GET"])
def admin_list_accounts():
    """List all multi-account entries (without secrets)."""
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    return jsonify({"accounts": auth.list_accounts()})


@app.route("/admin/accounts", methods=["POST"])
def admin_add_account():
    """Add a new multi-account entry.

    Body: {"api_key": "sk-...", "label": "My Account",
           "refresh_token": "1//...", "client_id": "...", "email": "..."}
    """
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    body = request.get_json(force=True, silent=True) or {}
    api_key = body.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400
    label = body.get("label", "").strip() or f"Account {len(auth._accounts) + 1}"
    refresh_token = body.get("refresh_token", "").strip()
    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 400
    client_id = body.get("client_id", "").strip()
    email = body.get("email", "").strip()
    result = auth.add_account(api_key, label, refresh_token, client_id, email)
    return jsonify(result)


@app.route("/admin/accounts/<path:api_key>", methods=["DELETE"])
def admin_remove_account(api_key: str):
    """Remove a multi-account entry by API key."""
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    result = auth.remove_account(api_key)
    if result.get("ok"):
        return jsonify(result)
    return jsonify(result), 404


# ── User endpoints ──────────────────────────────────────────


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


@app.route("/v1/usage")
def usage():
    """Return current credential status and account info."""
    if err := _check_api_key():
        return jsonify(err[0]), err[1]
    now_ms = int(time.time() * 1000)
    return jsonify({
        "email": auth.email,
        "subscription": auth.subscription,
        "token_expires_at": auth._expires_at,
        "token_remaining_ms": max(0, auth._expires_at - now_ms),
        "now_ms": now_ms,
        "accounts": len(auth._accounts),
        "token_endpoints": OAUTH_TOKEN_URLS,
    })


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

    headers = _anthropic_headers(extra_beta=extra_beta, model_id=model)

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
                    model_id=model,
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
                                "function": {"name": _unprefix_tool_name(block.get("name", "")), "arguments": ""},
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
        resp = _anthropic_request("POST", "/messages?beta=true", headers=headers,
                                   json=anthropic_req, model_id=model)
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
# PKCE Standalone OAuth Login (no OpenCode / Claude CLI needed)
# ============================================================
# Mirrors Hermes' run_hermes_oauth_login_pure() PKCE flow.
# Uses Anthropic's authorize endpoint with local callback server.

import webbrowser
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# OAuth PKCE endpoints (matches Hermes anthropic_adapter)
ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
ANTHROPIC_OAUTH_SCOPES = "org:create_api_key user:profile user:inference"

_pkce_state: dict[str, Any] = {}  # session_id -> {verifier, state, ...}


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


@app.route("/auth/login", methods=["POST"])
def auth_login_start():
    """Start Anthropic OAuth PKCE flow. Returns auth URL."""
    verifier, challenge = _generate_pkce()
    oauth_state = secrets.token_urlsafe(32)
    session_id = secrets.token_urlsafe(16)

    params = {
        "code": "true",
        "client_id": ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    }
    auth_url = f"{ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    _pkce_state[session_id] = {
        "verifier": verifier,
        "state": oauth_state,
        "created_at": time.time(),
    }

    # Open browser automatically
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    return jsonify({
        "session_id": session_id,
        "auth_url": auth_url,
        "message": "Open the URL in your browser, authorize, then POST the code to /auth/exchange",
    })


@app.route("/auth/exchange", methods=["POST"])
def auth_login_exchange():
    """Exchange authorization code for tokens (PKCE)."""
    body = request.get_json(force=True, silent=True) or {}
    session_id = body.get("session_id", "")
    auth_code = body.get("code", "").strip()
    api_key = body.get("api_key", "").strip()  # optional — for multi-account
    label = body.get("label", "Default").strip()

    sess = _pkce_state.get(session_id)
    if not sess:
        return jsonify({"error": "Unknown or expired session"}), 404

    if not auth_code:
        return jsonify({"error": "code is required"}), 400

    # Anthropic's callback appends #state to the code
    parts = auth_code.split("#", 1)
    code = parts[0]
    received_state = parts[1] if len(parts) > 1 else ""

    if received_state and received_state != sess["state"]:
        return jsonify({"error": "OAuth state mismatch — possible CSRF"}), 400

    # Exchange code for tokens
    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": ANTHROPIC_CLIENT_ID,
        "code": code,
        "state": received_state or sess["state"],
        "redirect_uri": ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()

    result = None
    last_error = None
    for endpoint in OAUTH_TOKEN_URLS:
        req_obj = urllib.request.Request(
            endpoint,
            data=exchange_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"claude-code/{CLAUDE_CODE_VERSION} (external, cli)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_obj, timeout=15) as resp_obj:
                result = json.loads(resp_obj.read().decode())
            break
        except Exception as e:
            last_error = e
            continue

    if result is None:
        return jsonify({"error": f"Token exchange failed: {last_error}"}), 502

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in", 3600))

    if not access_token:
        return jsonify({"error": "No access token in response"}), 502

    now_ms = int(time.time() * 1000)
    expires_at_ms = now_ms + (expires_in * 1000)

    # If api_key provided, add as multi-account; otherwise update default
    if api_key:
        auth.add_account(api_key, label, refresh_token,
                         client_id=ANTHROPIC_CLIENT_ID)
        # Persist the access token immediately
        cache_file = AUTH_CACHE_DIR / f"{hashlib.sha256(api_key.encode()).hexdigest()[:16]}.json"
        AUTH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(AUTH_CACHE_DIR), 0o700)
        except OSError:
            pass
        tmp = cache_file.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at_ms,
                }, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, cache_file)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        # Update default account
        auth._access = access_token
        auth._refresh = refresh_token
        auth._expires_at = expires_at_ms
        auth._persist()

    # Cleanup session
    _pkce_state.pop(session_id, None)

    return jsonify({
        "ok": True,
        "access_token_prefix": access_token[:12] + "...",
        "expires_at_ms": expires_at_ms,
        "has_refresh_token": bool(refresh_token),
    })


@app.route("/auth/status")
def auth_login_status():
    """Check OAuth login status."""
    session_id = request.args.get("session_id", "")
    if session_id and session_id in _pkce_state:
        return jsonify({"status": "pending", "session_id": session_id})

    return jsonify({
        "status": "authenticated" if auth._access else "not_authenticated",
        "email": auth.email,
        "subscription": auth.subscription,
        "token_expires_at": auth._expires_at,
        "now_ms": int(time.time() * 1000),
        "accounts": auth.list_accounts(),
    })


# ── Account switching persistence ───────────────────────────

_ACTIVE_ACCOUNT_FILE = Path(os.environ.get(
    "BRIDGE_ACTIVE_ACCOUNT_FILE",
    str(Path(__file__).resolve().parent / ".active_account"),
))


def _save_active_account(api_key_prefix: str) -> None:
    """Persist the active multi-account selection."""
    try:
        _ACTIVE_ACCOUNT_FILE.write_text(api_key_prefix)
    except Exception:
        pass


def _load_active_account() -> str | None:
    """Load persisted active account."""
    try:
        if _ACTIVE_ACCOUNT_FILE.exists():
            return _ACTIVE_ACCOUNT_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


@app.route("/admin/accounts/active", methods=["GET"])
def admin_get_active_account():
    """Get the currently active multi-account."""
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    active = _load_active_account()
    return jsonify({
        "active": active,
        "accounts": auth.list_accounts(),
    })


@app.route("/admin/accounts/active", methods=["POST"])
def admin_set_active_account():
    """Set the active multi-account by API key prefix."""
    if err := _admin_auth():
        return jsonify(err[0]), err[1]
    body = request.get_json(force=True, silent=True) or {}
    api_key_prefix = body.get("api_key_prefix", "").strip()
    if not api_key_prefix:
        return jsonify({"error": "api_key_prefix is required"}), 400
    _save_active_account(api_key_prefix)
    return jsonify({"ok": True, "active": api_key_prefix})


# ── Debug logging (CLAUDE_AUTH_DEBUG) ───────────────────────

_debug_log_path: Path | None = None
_debug_enabled = os.environ.get("CLAUDE_AUTH_DEBUG", "").strip()


def _debug_log(event: str, data: dict[str, Any] | None = None) -> None:
    """Log debug events when CLAUDE_AUTH_DEBUG is set."""
    global _debug_log_path
    if not _debug_enabled:
        return
    if _debug_log_path is None:
        log_dir = Path(os.environ.get("CLAUDE_AUTH_DEBUG_DIR",
                        str(Path(__file__).resolve().parent)))
        if _debug_enabled != "1":
            _debug_log_path = Path(_debug_enabled)
        else:
            _debug_log_path = log_dir / "claude-auth-debug.log"
    try:
        entry = {"ts": datetime.datetime.now().isoformat(), "event": event}
        if data:
            # Redact tokens
            redacted = {}
            for k, v in data.items():
                if k in ("refresh_token", "access_token", "refreshToken", "accessToken"):
                    redacted[k] = (v[:8] + "...REDACTED") if isinstance(v, str) and len(v) > 8 else "REDACTED"
                else:
                    redacted[k] = v
            entry.update(redacted)
        with open(_debug_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


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

    # Load multi-account config
    auth._load_accounts()
    if auth._accounts:
        print(f"[bridge] accounts   : {len(auth._accounts)} multi-account entries", flush=True)
        for api_key, acc in auth._accounts.items():
            print(f"  - {acc.get('label', '?'):20s} key={api_key[:16]}...", flush=True)

    print(f"[bridge] listening  : http://{args.host}:{args.port}", flush=True)
    try:
        models = fetch_available_models()
        print(f"[bridge] models     : {len(models)} available", flush=True)
    except Exception as e:
        print(f"[bridge] models     : static fallback ({e})", flush=True)

    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
