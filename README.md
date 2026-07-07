<div align="center">

# 🌑 Anthropic OAuth Bridge

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenAI compatible](https://img.shields.io/badge/OpenAI-compatible-412991?logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference)

**Turn your Claude Code / Anthropic OAuth session into an OpenAI-compatible API.**

Built for agents and clients that do **not** support Anthropic OAuth directly: **Hermes**, **OpenClaw**, **Open WebUI**, **Continue**, **Boba**, **BetterGPT**, and any other OpenAI-compatible tool.

</div>

---

## 📖 Table of contents

- [TL;DR](#tldr)
- [What is it?](#what-is-it)
- [Quick install](#quick-install)
- [Before you begin](#before-you-begin)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Multi-account support](#multi-account-support)
- [Agent setup](#agent-setup)
  - [Hermes](#hermes)
  - [OpenClaw](#openclaw)
  - [Open WebUI](#open-webui)
  - [Generic OpenAI client](#generic-openai-client)
- [Supported features](#supported-features)
  - [OAuth & Auth](#oauth--auth)
  - [Request transforms](#request-transforms)
  - [API compatibility](#api-compatibility)
  - [Robustness](#robustness)
  - [Admin & Ops](#admin--ops)
- [API reference](#api-reference)
- [Common mistakes](#common-mistakes)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## ⚡ TL;DR

Run this **one command**. It installs everything, logs you in, and starts the bridge as a service:

```bash
git clone https://github.com/lomeliDev/anthropic-oauth-bridge.git && \
cd anthropic-oauth-bridge && \
chmod +x install.sh scripts/*.sh && \
./install.sh
```

When the installer asks, just press **Enter** to accept the defaults. It will open your browser for Anthropic OAuth when needed.

> **You do NOT need to run `claude` or `opencode auth login` yourself.** The installer does it for you.

---

## ✨ What is it?

Your Claude Code CLI is already authenticated and keeps valid OAuth tokens in your system Keychain (macOS) or `~/.claude/.credentials.json` (Linux/Windows). The OpenCode plugin `opencode-claude-auth` synchronizes those credentials to `~/.local/share/opencode/auth.json`.

This small Flask bridge exposes those credentials through a clean **OpenAI-compatible HTTP API**, so you can reuse your Claude / Anthropic account from any client that speaks the OpenAI protocol.

```text
┌─────────────┐    OpenAI API      ┌────────────────────┐    HTTPS    ┌─────────────────────┐
│   Hermes    │ ─────────────────► │  Anthropic OAuth   │ ──────────► │  api.anthropic.com  │
│  OpenClaw   │   /v1/chat/...     │  Bridge :64173     │   Bearer   │  /v1/messages       │
│  Open WebUI │                    │                    │  OAuth     │  /v1/models         │
│  Continue   │                    │                    │            │                     │
└─────────────┘                    └────────────────────┘            └─────────────────────┘
```

### Why not just use the API key?

Because this bridge uses **OAuth tokens** — the same ones Claude Code uses internally. This means:

- **No API key billing** — uses your Claude subscription quota.
- **No separate billing** — same account as your Claude Code usage.
- **Automatic token refresh** — the bridge handles rotation, expiry, and race conditions with Claude Code itself.
- **Full model access** — all Anthropic models your account has access to (Sonnet, Opus, Haiku).

---

## 🚀 Quick install

```bash
git clone https://github.com/lomeliDev/anthropic-oauth-bridge.git
cd anthropic-oauth-bridge
chmod +x install.sh scripts/*.sh
./install.sh
```

The installer will:

1. Install the OpenCode CLI if it is missing.
2. Install the Claude Code CLI (`claude`) if it is missing.
3. Run `claude` for you if no session exists. *(A browser tab will open — just log in.)*
4. Add the `opencode-claude-auth` plugin to OpenCode if it is missing.
5. Run `opencode auth login` for you if no Anthropic OAuth credential exists. *(Another browser tab — same account.)*
6. Validate the Anthropic credential files.
7. Check Python 3.9+ and create a virtual environment.
8. Install Python dependencies.
9. Ask for a **port** (default `64173`).
10. Ask whether to enable an **API key / password** (generates a random one by default).
11. Detect your platform and install a **systemd** (Linux) or **launchd** (macOS) daemon automatically.
12. Run health / models validation tests.

> **Do not run `claude` or `opencode auth login` before `./install.sh`.** The installer handles the OAuth flows. If you already did them, the installer will detect them and skip those steps.

### Manual run (fallback)

If the installer cannot install a daemon, it creates a portable runner:

```bash
./daemon/run.sh
```

Or run directly:

```bash
source .env
.venv/bin/python3 server.py --host 0.0.0.0 --port 64173
```

---

## 🎯 Before you begin

The bridge **reuses** the Anthropic OAuth session created by Claude Code + OpenCode. You must complete these steps **once** before the bridge can authenticate with Anthropic.

**You can skip this section if you run `./install.sh`.** The installer does everything below automatically. This section is only for people who want to set things up manually.

### 1. Install the OpenCode CLI

```bash
curl -fsSL https://opencode.ai/install | bash
opencode --version
```

### 2. Install the Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

If you do not have `npm`, the installer can install Node.js via `nvm` for you.

### 3. Log in with Claude Code

```bash
claude
```

Follow the OAuth flow in the browser. When you see `Logged in as <email>`, you are done.

### 4. Add the Anthropic auth plugin to OpenCode

```bash
mkdir -p ~/.config/opencode
cat > ~/.config/opencode/opencode.jsonc <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["opencode-claude-auth@latest"]
}
EOF
```

### 5. Log in with OpenCode

```bash
opencode auth login
```

Select:

- **Provider:** `Anthropic`

The plugin will read the credentials from Claude Code and synchronize them.

### 6. Verify the credential was stored

```bash
opencode auth list
```

You should see an `Anthropic oauth` entry.

### 7. Where the credential files live

After login you will have:

```text
~/.claude/.credentials.json                (Linux/Windows primary source)
Keychain → "Claude Code-credentials"       (macOS primary source)
~/.local/share/opencode/auth.json          (OpenCode sync target)
```

---

## 📋 Requirements

| Requirement | Details |
|-------------|---------|
| **Python** | 3.9 or newer |
| **OS** | Linux (systemd recommended) or macOS |
| **Node.js + npm** | Required to install Claude Code CLI (installer can install via nvm) |
| **OpenCode CLI** | Installed (`curl -fsSL https://opencode.ai/install \| bash`) |
| **Claude Code CLI** | Installed and logged in (`claude`) |
| **Anthropic auth plugin** | `opencode-claude-auth` configured in `~/.config/opencode/opencode.jsonc` |
| **OpenCode auth** | `opencode auth login` completed with Anthropic |
| **Credential files** | `~/.local/share/opencode/auth.json` with an `anthropic` entry |

If any prerequisite is missing, the installer stops and tells you exactly what to do. The installer can automatically install Node.js via `nvm` if `npm` is missing.

---

## ⚙️ Configuration

The bridge is configured through environment variables. The installer writes them to `.env`.

Copy the example file and edit:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST` | `127.0.0.1` | Listen host |
| `PORT` | `64173` | Listen port |
| `BRIDGE_API_KEY` | *(none)* | Optional API key required from clients (`Authorization: Bearer <key>`) |
| `BRIDGE_ADMIN_KEY` | *(none)* | API key for admin endpoints (`/admin/*`). Leave empty for dev mode. |
| `ANTHROPIC_AUTH_PATH` | `~/.local/share/opencode/auth.json` | OpenCode auth file |
| `CLAUDE_CREDENTIALS_PATH` | `~/.claude/.credentials.json` | Claude Code credentials fallback |
| `ANTHROPIC_CLIENT_ID` | `9d1c250a-...` | Public OAuth client id |
| `BRIDGE_ACCOUNTS_FILE` | `./accounts.json` | Multi-account config file |
| `BRIDGE_AUTH_CACHE_DIR` | `./auth_cache/` | Per-account token cache directory |
| `ANTHROPIC_CLI_VERSION` | `2.1.112` | Claude Code version for billing header |
| `CLAUDE_CODE_ENTRYPOINT` | `sdk-cli` | Entrypoint for billing header |
| `CLAUDE_AUTH_DEBUG` | *(empty)* | Set to `"1"` for debug logging |

Edit `.env` and restart the service to apply changes:

```bash
# Linux
sudo systemctl restart anthropic-oauth-bridge

# macOS
launchctl stop com.lomelidev.anthropic-oauth-bridge
launchctl start com.lomelidev.anthropic-oauth-bridge
```

---

## 👥 Multi-account support

The bridge supports **multiple Anthropic accounts** behind a single instance. Each account gets its own API key — clients switch accounts by using a different API key in the `Authorization` header.

### How it works

```text
┌─────────────┐  Bearer sk-aaa  ┌────────────────────┐  OAuth token A  ┌────────────────┐
│ Client A    │ ──────────────► │                    │ ──────────────► │ Account A      │
└─────────────┘                 │  Anthropic OAuth   │                 └────────────────┘
                                │  Bridge :64173     │
┌─────────────┐  Bearer sk-bbb  │                    │  OAuth token B  ┌────────────────┐
│ Client B    │ ──────────────► │                    │ ──────────────► │ Account B      │
└─────────────┘                 └────────────────────┘                 └────────────────┘
```

### Adding an account

Add accounts via the **admin API**:

```bash
# Add a new account
curl -s -X POST http://127.0.0.1:64173/admin/accounts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${BRIDGE_ADMIN_KEY}" \
  -d '{
    "api_key": "sk-personal-account",
    "label": "Personal",
    "refresh_token": "1//your-google-refresh-token",
    "email": "you@gmail.com"
  }'
```

> **Tip:** Set `BRIDGE_ADMIN_KEY` in `.env` to protect the admin API. Without it, the admin API is open — fine for local dev, not for remote deployment.

### Login via built-in PKCE OAuth

The bridge has a **built-in PKCE OAuth flow** — no need for Claude Code CLI or OpenCode:

```bash
# Start the login flow (opens browser)
curl -s -X POST http://127.0.0.1:64173/auth/login | jq
# → {"session_id": "...", "auth_url": "https://..."}

# After authorizing in the browser, exchange the code:
curl -s -X POST http://127.0.0.1:64173/auth/exchange \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<from-login>",
    "code": "<from-browser-url>",
    "api_key": "sk-personal-account",
    "label": "Personal"
  }'
```

### Managing accounts

```bash
# List all accounts (without secrets)
curl -s http://127.0.0.1:64173/admin/accounts \
  -H "Authorization: Bearer ${BRIDGE_ADMIN_KEY}"

# Remove an account
curl -s -X DELETE http://127.0.0.1:64173/admin/accounts/sk-personal-account \
  -H "Authorization: Bearer ${BRIDGE_ADMIN_KEY}"

# Check auth status (includes account list)
curl -s http://127.0.0.1:64173/auth/status
```

### Using accounts from clients

```bash
# Use a specific account
curl -s http://127.0.0.1:64173/v1/models \
  -H "Authorization: Bearer sk-personal-account"

# Chat with a specific account
curl -s http://127.0.0.1:64173/v1/chat/completions \
  -H "Authorization: Bearer sk-personal-account" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
```

Each account's tokens are cached independently in `auth_cache/`. Refresh tokens are rotated and persisted back to `accounts.json`.

### Account file format

See [`accounts.example.json`](accounts.example.json) for the format:

```json
{
  "accounts": {
    "sk-personal": {
      "label": "Personal Account",
      "refresh_token": "1//...",
      "client_id": "9d1c250a-...",
      "email": "you@gmail.com"
    }
  }
}
```

---

## 🤖 Agent setup

The bridge is meant to be consumed by agents and clients that do not support Anthropic OAuth themselves.

### Hermes

Run the helper script after `./install.sh`:

```bash
chmod +x scripts/add-to-hermes.sh
./scripts/add-to-hermes.sh
```

The script will ask for the model and provider name, or you can pass them as arguments:

```bash
./scripts/add-to-hermes.sh claude-opus-4-1 my-anthropic
```

What it does:

- Validates `.env`, `PORT`, the bridge venv, and Hermes installation.
- **Generates a bridge API key automatically** if you did not set one, and restarts the bridge service.
- Registers a **named provider** in the top-level `providers` map of `~/.hermes/config.yaml` (Hermes' preferred format).
- Keeps `custom_providers` in sync for older Hermes versions.
- Optionally sets the provider as active.
- Removes stale `model.base_url` / `model.api_key` entries that could point to OpenRouter.
- Creates a backup of your config and validates the generated YAML.
- Optionally restarts the Hermes gateway and shows status before/after.

> **Note:** If you see a warning about `model.base_url` still pointing to OpenRouter, the script could not clean it automatically. Edit `~/.hermes/config.yaml` and remove `model.base_url` / `model.api_key` so Hermes uses your named provider.

It creates a **named custom provider** in `~/.hermes/config.yaml` so it does not collide with other `custom` endpoints:

```yaml
custom_providers:
  - name: anthropic-oauth-bridge
    base_url: http://127.0.0.1:64173/v1
    api_key: your-bridge-api-key          # only if you enabled auth
    api_mode: chat_completions
    models:
      - id: claude-sonnet-4-5
        name: claude-sonnet-4-5

model:
  provider: custom:anthropic-oauth-bridge
  default: claude-sonnet-4-5
```

Then start Hermes and switch models with:

```bash
/model custom:anthropic-oauth-bridge:claude-sonnet-4-5
```

### OpenClaw

Run the helper script after `./install.sh`:

```bash
chmod +x scripts/add-to-openclaw.sh
./scripts/add-to-openclaw.sh
```

The script will ask for the model and provider name, or you can pass them as arguments:

```bash
./scripts/add-to-openclaw.sh claude-opus-4-1 my-anthropic
```

What it does:

- Validates `.env`, `PORT`, and `python3`.
- **Generates a bridge API key automatically** if you did not set one, and restarts the bridge service.
- Edits `~/.openclaw/openclaw.json` (creating it if necessary) and adds the bridge as a custom provider.
- Creates a backup of your config and validates the generated JSON.
- Optionally applies the config with `openclaw gateway config.apply`, restarts the OpenClaw gateway, and shows status before/after.

It edits `~/.openclaw/openclaw.json` (creating it if necessary) and adds the bridge as a custom provider:

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "anthropic-oauth-bridge": {
        "baseUrl": "http://127.0.0.1:64173/v1",
        "api": "openai-completions",
        "apiKey": "your-bridge-api-key",
        "models": [{ "id": "claude-sonnet-4-5", "name": "claude-sonnet-4-5" }]
      }
    }
  },
  "agents": {
    "defaults": {
      "models": {
        "anthropic-oauth-bridge/claude-sonnet-4-5": { "alias": "claude-sonnet-4-5" }
      }
    }
  }
}
```

Apply the config and restart the gateway:

```bash
openclaw gateway config.apply --file ~/.openclaw/openclaw.json
```

Then in chat:

```bash
/model claude-sonnet-4-5
```

### Open WebUI

1. Go to **Admin Panel → Settings → Connections**.
2. Add an OpenAI API connection.
3. Set **URL** to `http://YOUR_SERVER_IP:64173/v1`.
4. Set **Key** to your `BRIDGE_API_KEY` (or any placeholder if auth is disabled).
5. Save — the model list will populate automatically.

### Generic OpenAI client

| Field | Value |
|-------|-------|
| **Base URL** | `http://YOUR_SERVER_IP:64173/v1` |
| **API key** | Your `BRIDGE_API_KEY` value, or any non-empty string if auth is disabled |
| **Models** | Fetched automatically from `GET /v1/models` |

---

## 🧩 Supported features

### OAuth & Auth

| Feature | Description |
|---------|-------------|
| **PKCE standalone login** | Built-in OAuth flow via `POST /auth/login` + `POST /auth/exchange` — no Claude Code CLI needed |
| **5-source credential resolution** | auth.json → credentials.json → accounts.json → BRIDGE_REFRESH_TOKEN → PKCE login |
| **Multi-account pool** | Multiple Anthropic accounts behind a single bridge; switch via API key |
| **Race-safe token refresh** | Re-reads credentials before refreshing to avoid racing with Claude Code |
| **Multi-endpoint refresh** | Tries `platform.claude.com` then `console.anthropic.com` |
| **Client API key auth** | Optional `BRIDGE_API_KEY` for bridge-level auth |
| **Admin API auth** | `BRIDGE_ADMIN_KEY` protects admin endpoints |
| **Session persistence** | Refreshed tokens written back to both `auth.json` and `credentials.json` |

### Request transforms

| Feature | Description |
|---------|-------------|
| **Billing header** | Cryptographic `x-anthropic-billing-header` with SHA-256 hash matching Claude Code |
| **System identity injection** | Injects "You are Claude Code…" as standalone system entry |
| **System prompt relocation** | Non-identity system content moved to first user message |
| **System sanitization** | Replaces product names (Hermes → Claude Code) for Anthropic content filters |
| **ALL tools mcp__ prefix** | Both bare tools and MCP tools get `mcp__` prefix required by OAuth |
| **Tool PascalCase** | Tool names in requests and conversation history transformed |
| **Orphan tool repair** | Removes tool_use/tool_result blocks with missing counterparts |
| **Effort stripping (Haiku)** | Strips `thinking.effort` and `output_config.effort` for Haiku models |
| **Fast mode (Opus 4.6)** | `speed=fast` for 2.5× throughput on Opus 4.6 |
| **Temperature stripping (4.7+)** | Strips sampling params for Opus 4.7+ models that reject them |
| **Cache token reporting** | Surfaces `cache_creation_tokens` and `cache_read_tokens` in usage |

### Betas

| Feature | Description |
|---------|-------------|
| **Dynamic betas per model** | Model-specific beta sets (Haiku, Opus 4.6, Opus 4.7+) |
| **Auto-exclusion recovery** | Detects long-context errors and retries without offending betas |
| **Common betas** | `interleaved-thinking`, `fine-grained-tool-streaming` for all requests |
| **OAuth-only betas** | `claude-code-20250219`, `oauth-2025-04-20` for OAuth-bridged requests |
| **Effort beta** | `effort-2025-11-24` for Opus 4.6/4.7 |
| **Fast mode beta** | `fast-mode-2026-02-01` for Opus 4.6 |
| **Long context beta** | `context-1m-2025-08-07` with auto-exclusion on error |

### API compatibility

| Feature | Description |
|---------|-------------|
| `GET /health` | Health check with email, subscription, token expiry, account count |
| `GET /v1/models` | Dynamic model list fetched from Anthropic API, cached 5 min |
| `POST /v1/chat/completions` | Blocking and SSE streaming |
| Tools / functions | `tools`, `tool_choice`, multi-turn `role: "tool"` |
| JSON Schema sanitization | Removes Anthropic-rejected keywords, inlines `$ref`, handles `oneOf`/`allOf` |
| Vision | `image_url` with base64 data URI or public http(s) URL |
| PDF documents | Base64 document support |
| `thinking` | Extended thinking for Sonnet/Opus |
| `response_format` | `json_object` and `json_schema` via forced tool |
| `stream_options.include_usage` | Token usage in final stream chunk |
| OpenAI params | `seed`, `max_tokens`, `max_completion_tokens`, `n`, `stop`, `temperature`, `top_p`, `top_k` |

### Robustness

| Feature | Description |
|---------|-------------|
| **Fetch retry ladder** | 401 → refresh + retry; 429/529 → exponential backoff (30s cap); 400 long-context → exclude beta + retry |
| **Stainless headers** | `x-stainless-arch/lang/os/package-version/retry-count/runtime/timeout` for SDK fingerprint |
| **Retry-after respect** | Honours upstream `retry-after` headers, caps at 30s |
| **User-Agent fix** | Uses `claude-code/` not `claude-cli/` (Anthropic blocks the latter) |
| **Session ID** | Stable `X-Claude-Code-Session-Id` per process |
| **Atomic file writes** | O_EXCL temp files → atomic rename for credential persistence |
| **Error surfaces** | `message_start` usage, `message_delta` usage, and error chunks in streaming |

### Admin & Ops

| Feature | Description |
|---------|-------------|
| `GET /admin/health` | Full health with credential status and account list |
| `GET /admin/accounts` | List all accounts (secrets redacted) |
| `POST /admin/accounts` | Add a multi-account entry |
| `DELETE /admin/accounts/<key>` | Remove an account |
| `GET /admin/accounts/active` | Get/set active account |
| `GET /v1/usage` | Token status, email, subscription, expiry |
| `GET /auth/status` | OAuth login status + account list |
| `POST /auth/login` | Start PKCE OAuth flow |
| `POST /auth/exchange` | Exchange code for tokens |
| Debug logging | `CLAUDE_AUTH_DEBUG=1` for event tracing with token redaction |

> **Note:** `logprobs`, `frequency_penalty`, `presence_penalty`, and `logit_bias` are not supported by the Anthropic upstream and are silently ignored.

---

## 🧪 API quick tests

```bash
BASE=http://127.0.0.1:64173
KEY="your-bridge-api-key-or-empty"
AUTH=""
[ -n "$KEY" ] && AUTH="-H Authorization: Bearer ***"

# Health check
curl -s $AUTH "$BASE/health" | jq

# List models
curl -s $AUTH "$BASE/v1/models" | jq '.data[].id'

# Chat completion
curl -s "$BASE/v1/chat/completions" \
  $AUTH \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "hello"}]
  }' | jq

# Streaming
curl -N "$BASE/v1/chat/completions" \
  $AUTH \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 200,
    "stream": true,
    "messages": [{"role": "user", "content": "tell me a joke"}]
  }'

# Tool use
curl -s "$BASE/v1/chat/completions" \
  $AUTH \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 256,
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
      }
    }],
    "messages": [{"role": "user", "content": "weather in CDMX?"}]
  }' | jq

# PKCE login (no Claude Code needed)
curl -s -X POST "$BASE/auth/login" | jq

# Multi-account — add an account
curl -s -X POST "$BASE/admin/accounts" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${BRIDGE_ADMIN_KEY}" \
  -d '{"api_key":"sk-xxx","label":"Work","refresh_token":"1//..."}' | jq

# Multi-account — use a specific account
curl -s -H "Authorization: Bearer sk-xxx" "$BASE/v1/models" | jq '.data[].id'

# Usage / token status
curl -s $AUTH "$BASE/v1/usage" | jq
```

---

## 🚫 Common mistakes

| Mistake | Why it fails | What to do |
|---------|--------------|------------|
| Running `claude` or `opencode auth login` manually before `./install.sh` | Nothing breaks, but it is unnecessary. The installer does it automatically and skips the steps if it detects a session. | Just run `./install.sh`. |
| Closing the terminal during the browser OAuth flow | The installer waits for you to come back. If you close it, the login never finishes. | Re-run `./install.sh`. |
| Running `./install.sh` with `sudo` | The bridge will be configured for `root` and the service will run as `root`, which is usually not what you want. | Run as your normal user. |
| Picking a port that is already in use | The bridge cannot start. | Re-run `./install.sh` and choose a different port, or stop the other service. |
| Forgetting the `BRIDGE_API_KEY` when connecting a client | You get `401 Unauthorized`. | Copy the key from `.env` or disable auth by removing `BRIDGE_API_KEY` from `.env`. |
| Using a different Anthropic account for `claude` and `opencode auth login` | The credentials may not match and the bridge can fail to refresh tokens. | Use the **same** Anthropic account for both logins. |

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| `opencode CLI not found` | Install OpenCode first: https://opencode.ai |
| `opencode-claude-auth plugin is not configured` | Run the installer and let it add the plugin, or add it manually as shown in [Before you begin](#before-you-begin). |
| `No Anthropic credentials found` | Run `opencode auth login`, select Anthropic, and finish the browser login. |
| `claude command not found` | The installer can install it via `npm install -g @anthropic-ai/claude-code`. If npm is missing, it installs Node.js via nvm first. |
| Port already in use | Pick a different port during install or stop the other service. |
| `401 Unauthorized` | Set `Authorization: Bearer <key>` in your client, or disable the API key in `.env`. |
| Models list is empty | The bridge could not refresh the Anthropic token. Check `bridge.log` and verify the credential files are valid. |
| Service fails to start | Run the bridge manually to see the error: `source .env && .venv/bin/python3 server.py` |
| `OAuth refresh error: invalid_grant` | Your refresh token was revoked. Re-run `claude` to log in again, then `opencode auth login`. |
| `Provider already exists` in `add-to-hermes.sh` / `add-to-openclaw.sh` | The script protects your config from accidental overwrites. | Run the script with `--force` or choose a different provider name. |
| `Hermes config is not valid YAML` | The script detected a YAML syntax problem after editing. | A backup was created; restore it and report the issue. |
| `OpenClaw config is not valid JSON` | The script detected a JSON syntax problem after editing. | A backup was created; restore it and report the issue. |
| 400 `Extra usage is required` | Long-context beta was rejected. The bridge auto-excludes it and retries. | The next request will work. |
| Tools not working / 400 `invalid tool name` | Tool names must have `mcp__` prefix. The bridge applies it automatically. | If you're sending raw Anthropic requests, prefix tools with `mcp__`. |

---

## 🏗️ Architecture

```text
server.py  (2,246 lines — single-file Flask app)
├── Auth class          — 5-source credential resolution + multi-account pool
├── Beta system         — dynamic per-model betas with auto-exclusion recovery
├── Request transforms  — billing header, system identity, tool naming, sanitization
├── OpenAI<->Anthropic  — full message/schema/tool conversion
├── Streaming engine    — SSE streaming with tool call reconstruction
├── Multi-account admin — REST API for account CRUD
├── PKCE OAuth login    — standalone OAuth flow (no Claude Code needed)
├── Retry engine        — 401 refresh, 429 backoff, 400 beta recovery
└── Debug logging       — event tracing with automatic token redaction
```

**Credential resolution order** (matches Hermes `anthropic_adapter.py`):
1. `~/.local/share/opencode/auth.json` (OpenCode-synced)
2. `~/.claude/.credentials.json` (Claude Code native)
3. `accounts.json` (multi-account pool)
4. `BRIDGE_REFRESH_TOKEN` env var (legacy single-account)
5. Built-in PKCE OAuth flow (`POST /auth/login` → `POST /auth/exchange`)

**Token refresh flow:**
1. Check if cached token is still valid (>30s buffer)
2. Re-read credential files (race-safe — catches Claude Code rotations)
3. Try `platform.claude.com/v1/oauth/token` first, then `console.anthropic.com`
4. Atomic write back to both `auth.json` and `credentials.json`

---

## ⚠️ Disclaimer

This is an **unofficial, experimental** project. The author is not affiliated with Anthropic, Claude Code, Claude, OpenCode, Hermes, OpenClaw, or any other mentioned service.

By installing and using this software you agree that:

- You use it **at your own risk**.
- The author is **not responsible** for account bans, suspensions, rate-limit issues, data loss, security incidents, or any other consequences.
- You are solely responsible for complying with the terms of service of any third-party service you access through this bridge.

The author created this project "just for fun". Be conscious of what you do with it.

See [DISCLAIMER.md](DISCLAIMER.md) for the full text.

---

## 📄 License

[MIT](LICENSE) © [@lomeliDev](https://github.com/lomeliDev)

---

<div align="center">

**Made with 🖤 so you can use Claude everywhere.**

</div>
