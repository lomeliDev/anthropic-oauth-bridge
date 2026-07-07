<div align="center">

# 🌑 Anthropic OAuth Bridge

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenAI compatible](https://img.shields.io/badge/OpenAI-compatible-412991?logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference)

**Turn your Anthropic OAuth session into an OpenAI-compatible API.**

No API key billing. No separate account. Uses your Claude subscription.

</div>

---

## 📖 Table of contents

- [TL;DR](#-tldr)
- [What is it?](#-what-is-it)
- [Quick install](#-quick-install)
- [Authentication (built-in PKCE)](#-authentication-built-in-pkce)
- [Multi-account support](#-multi-account-support)
- [Agent setup](#-agent-setup)
- [API reference](#-api-reference)
- [Configuration](#-configuration)
- [Troubleshooting](#-troubleshooting)
- [Architecture](#-architecture)

---

## ⚡ TL;DR

```bash
git clone https://github.com/lomeliDev/anthropic-oauth-bridge.git
cd anthropic-oauth-bridge
chmod +x install.sh
./install.sh
```

The installer creates a virtualenv, installs deps, and starts the bridge. Then authenticate:

```bash
python3 auth-login.py
```

Open the URL in your browser, authorize, and pass the code back. The bridge is ready.

> **No Claude Code CLI or OpenCode needed.** The bridge has a built-in PKCE OAuth flow.

---

## ✨ What is it?

A Flask bridge that exposes your Anthropic OAuth session through an **OpenAI-compatible API** (`/v1/chat/completions`, `/v1/models`, etc.). Any client that speaks OpenAI can use it: **Hermes**, **Open WebUI**, **Continue**, **BetterGPT**, and more.

```
┌─────────────┐    OpenAI API      ┌────────────────────┐    HTTPS    ┌─────────────────────┐
│   Hermes    │ ─────────────────► │  Anthropic OAuth   │ ──────────► │  api.anthropic.com  │
│  Open WebUI │   /v1/chat/...     │  Bridge :64173     │   Bearer   │  /v1/messages       │
│  Continue   │                    │                    │  OAuth     │  /v1/models         │
└─────────────┘                    └────────────────────┘            └─────────────────────┘
```

### Why not just use an API key?

Because this uses **OAuth tokens** — the same ones Claude Code uses internally:

- **No API key billing** — uses your Claude subscription quota.
- **No separate billing** — same account as Claude Code.
- **Automatic token refresh** — on real 401, with exponential backoff.
- **Full model access** — all models your account has access to.

---

## 🚀 Quick install

```bash
git clone https://github.com/lomeliDev/anthropic-oauth-bridge.git
cd anthropic-oauth-bridge
./install.sh
```

The installer:
1. Checks Python 3.9+
2. Creates virtual environment, installs `flask`, `requests`, `pysocks`
3. Prompts for port (default `64173`) and optional API key
4. Installs and starts a systemd/launchd daemon

### Manual run (no daemon)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env if needed
.venv/bin/python3 server.py --port 64173
```

---

## 🔑 Authentication (built-in PKCE)

The bridge has a **built-in PKCE OAuth flow**. No external tools needed.

### Option A: Using auth-login.py (recommended)

```bash
# Step 1: Generate URL
python3 auth-login.py
# → Opens a URL like https://claude.ai/oauth/authorize?code=true&...

# Open the URL in your browser, click "Authorize"
# Copy the redirect URL (or just the code after ?code=)

# Step 2: Exchange
python3 auth-login.py <code-from-url>
# ✅ Tokens saved to ~/.claude/.credentials.json
```

### Option B: Using the bridge API

```bash
# Step 1: Get auth URL
curl -s -X POST http://127.0.0.1:64173/auth/login | jq
# → {"session_id": "...", "auth_url": "https://...", "code_verifier": "..."}

# Step 2: Open auth_url in browser, authorize, copy code from redirect URL

# Step 3: Exchange (server-side — claude.ai endpoint avoids rate limits)
curl -s -X POST http://127.0.0.1:64173/auth/exchange \
  -H "Content-Type: application/json" \
  -d '{"session_id":"...","code":"..."}' | jq
# ✅ Tokens saved

# If you get 429 (rate limited), run the exchange on your local machine:
curl -s https://claude.ai/v1/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=<CODE>&client_id=9d1c250a-...&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&code_verifier=<VERIFIER>"

# Then save the tokens:
curl -s -X POST http://127.0.0.1:64173/auth/save-tokens \
  -H "Content-Type: application/json" \
  -d '{"access_token":"sk-ant-oat01-...","refresh_token":"sk-ant-ort01-...","expires_in":28800}'
```

### Using Claude Code credentials (optional)

If you already have Claude Code CLI installed and authenticated:

```bash
# macOS
security find-generic-password -a "$USER" -s "Claude Code-credentials" -w > ~/.claude/.credentials.json

# Linux (Claude Code writes here automatically)
cat ~/.claude/.credentials.json
```

---

## 👥 Multi-account support

Run multiple Anthropic accounts behind a single bridge. Each account gets its own API key.

```
Client A → Authorization: Bearer sk-account-a → Account A
Client B → Authorization: Bearer sk-account-b → Account B
```

### Adding an account via PKCE

```bash
# Start PKCE flow
curl -s -X POST http://127.0.0.1:64173/auth/login | jq
# Open auth_url in browser (logged into the desired account), copy code

# Exchange with api_key to register as multi-account
curl -s -X POST http://127.0.0.1:64173/auth/exchange \
  -H "Content-Type: application/json" \
  -d '{"session_id":"...","code":"...","api_key":"sk-myaccount","label":"My Account"}'
```

### Using accounts

```bash
# Default account (no API key or BRIDGE_API_KEY)
curl -s http://127.0.0.1:64173/v1/models

# Specific account
curl -s http://127.0.0.1:64173/v1/models \
  -H "Authorization: Bearer sk-myaccount"
```

---

## 🤖 Agent setup

### Hermes

```bash
chmod +x scripts/add-to-hermes.sh
./scripts/add-to-hermes.sh
```

Creates a named provider in `~/.hermes/config.yaml`:

```yaml
custom_providers:
  - name: anthropic-oauth-bridge
    base_url: http://127.0.0.1:64173/v1
    api_key: your-bridge-api-key
    api_mode: chat_completions
    models:
      - id: claude-sonnet-4-5
```

Then in Hermes: `/model custom:anthropic-oauth-bridge:claude-sonnet-4-5`

### Open WebUI

Admin Panel → Settings → Connections → Add OpenAI API:
- **URL:** `http://YOUR_IP:64173/v1`
- **Key:** Your `BRIDGE_API_KEY` (or any value if auth disabled)

### Generic OpenAI client

| Field | Value |
|-------|-------|
| Base URL | `http://YOUR_IP:64173/v1` |
| API key | `BRIDGE_API_KEY` or any string |
| Models | Auto-fetched from `GET /v1/models` |

---

## 📡 API reference

### Inference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | Chat (streaming + non-streaming) |
| `GET` | `/v1/usage` | Token status |

### Auth (PKCE)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/auth/login` | Start PKCE flow → returns `auth_url`, `session_id`, `code_verifier` |
| `POST` | `/auth/exchange` | Exchange code for tokens |
| `POST` | `/auth/save-tokens` | Save tokens from client-side exchange (for rate-limited IPs) |
| `GET` | `/auth/status` | Check auth status + account list |

### Admin (multi-account)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/accounts` | List accounts |
| `POST` | `/admin/accounts` | Add account |
| `DELETE` | `/admin/accounts/<key>` | Remove account |

### Quick tests

```bash
BASE=http://127.0.0.1:64173

# Health
curl -s $BASE/health | jq

# Models
curl -s $BASE/v1/models | jq '.data[].id'

# Chat
curl -s $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":100,"messages":[{"role":"user","content":"hello"}]}' | jq

# Streaming
curl -N $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","stream":true,"messages":[{"role":"user","content":"tell me a joke"}]}'
```

---

## ⚙️ Configuration

Environment variables (`.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `64173` | Listen port |
| `BRIDGE_API_KEY` | *(none)* | API key required from clients |
| `CLAUDE_CREDENTIALS_PATH` | `~/.claude/.credentials.json` | OAuth credential source |
| `ANTHROPIC_CLIENT_ID` | `9d1c250a-...` | Public OAuth client ID |
| `ANTHROPIC_CLI_VERSION` | `2.1.202` | Claude Code version for headers |
| `CLAUDE_AUTH_DEBUG` | *(empty)* | Set to `"1"` for debug logging |

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| `401 Unauthorized` | Set `Authorization: Bearer <key>` or disable API key in `.env` |
| `Rate limited` on auth exchange | Run the exchange `curl` from your local machine (not the server) |
| Models list empty | Token expired. Re-authenticate with `python3 auth-login.py` |
| Port already in use | Change `PORT` in `.env` and restart |
| Tools not working | Tool names get `mcp__` prefix automatically (Anthropic OAuth requirement) |

---

## 🏗️ Architecture

```
server.py  (~2,350 lines — single-file Flask app)
├── Auth class          — credential resolution + multi-account pool + refresh
├── Beta system         — dynamic per-model betas with auto-exclusion recovery
├── Request transforms  — billing header, system identity, tool naming, sanitization
├── OpenAI↔Anthropic   — message/schema/tool conversion
├── Streaming engine    — SSE streaming with tool call reconstruction
├── PKCE OAuth login    — standalone flow (no Claude Code needed)
├── Retry engine        — 401 refresh + backoff, 429 backoff (30s cap)
└── Admin API           — multi-account CRUD
```

**Credential resolution order:**
1. `~/.claude/.credentials.json` (Claude Code native / PKCE output)
2. `accounts.json` (multi-account pool)
3. Built-in PKCE OAuth flow (`POST /auth/login` → `POST /auth/exchange`)

**Token refresh:**
1. Use token optimistically (no proactive refresh — avoids Anthropic cooldown rate limits)
2. On 401 → refresh with exponential backoff (0s → 2s → 4s → 8s → 16s → 32s)
3. `claude.ai/v1/oauth/token` for exchange and refresh (avoids `platform.claude.com` rate limits)
4. Race-safe: re-reads credential files before refreshing

---

## ⚠️ Disclaimer

This is an **unofficial, experimental** project. Not affiliated with Anthropic.

By using this software you:
- Use it **at your own risk**
- Are responsible for complying with Anthropic's terms of service
- Accept that the author is not responsible for bans, rate limits, or any consequences

---

## 📄 License

[MIT](LICENSE) © [@lomeliDev](https://github.com/lomeliDev)
