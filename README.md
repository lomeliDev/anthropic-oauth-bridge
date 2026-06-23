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
- [Agent setup](#agent-setup)
  - [Hermes](#hermes)
  - [OpenClaw](#openclaw)
  - [Open WebUI](#open-webui)
  - [Generic OpenAI client](#generic-openai-client)
- [Supported features](#supported-features)
- [API quick tests](#api-quick-tests)
- [Common mistakes](#common-mistakes)
- [Troubleshooting](#troubleshooting)
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

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST` | `127.0.0.1` | Listen host |
| `PORT` | `64173` | Listen port |
| `BRIDGE_API_KEY` | *(none)* | Optional API key required from clients (`Authorization: Bearer <key>`) |
| `ANTHROPIC_AUTH_PATH` | `~/.local/share/opencode/auth.json` | OpenCode auth file |
| `CLAUDE_CREDENTIALS_PATH` | `~/.claude/.credentials.json` | Claude Code credentials fallback |
| `ANTHROPIC_CLIENT_ID` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` | Public OAuth client id |

Edit `.env` and restart the service to apply changes:

```bash
# Linux
sudo systemctl restart anthropic-oauth-bridge

# macOS
launchctl stop com.lomelidev.anthropic-oauth-bridge
launchctl start com.lomelidev.anthropic-oauth-bridge
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

You can pass a different provider name if you already have other OAuth plugins:

```bash
./scripts/add-to-hermes.sh claude-opus-4-1 my-anthropic
```

The script validates that:

- `.env` exists and contains `PORT`.
- The bridge Python venv exists.
- Hermes is installed.
- The generated YAML is syntactically valid.
- A backup of your existing config is created before editing.

If the provider already exists, it will ask you to use `--force` to overwrite:

```bash
./scripts/add-to-hermes.sh --force
```

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

You can pass a different provider name and model if you already have other OAuth plugins:

```bash
./scripts/add-to-openclaw.sh claude-opus-4-1 my-anthropic
```

The script validates that:

- `.env` exists and contains `PORT`.
- `python3` is available.
- The generated JSON is syntactically valid.
- A backup of your existing config is created before editing.

If the provider already exists, use `--force` to overwrite:

```bash
./scripts/add-to-openclaw.sh --force
```

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

- `GET /health`
- `GET /v1/models` and `GET /v1/models/{id}` (dynamically fetched from Anthropic)
- `POST /v1/chat/completions` (blocking and SSE streaming)
- Tools / functions (`tools`, `tool_choice`, multi-turn `role: "tool"`)
- **Automatic JSON Schema sanitization** for tools and `response_format`, removing Anthropic-rejected keywords (`$schema`, `exclusiveMinimum`, `pattern`, `format`, `minLength`, etc.) and rewriting `oneOf`/`allOf` to `anyOf` or merged properties.
- Vision (`image_url` with base64 data URI or public http(s) URL)
- PDF documents (base64)
- `thinking` parameter for Sonnet/Opus extended thinking
- `response_format` (`json_object` and `json_schema` via forced tool)
- `seed`, `max_tokens`, `max_completion_tokens`, `n`, `stop`, `temperature`, `top_p`, `top_k`
- `stream_options.include_usage`
- Optional `BRIDGE_API_KEY` client authentication

> **Note:** `logprobs`, `frequency_penalty`, `presence_penalty`, and `logit_bias` are not supported by the Anthropic upstream and are silently ignored.

---

## 🧪 API quick tests

```bash
BASE=http://127.0.0.1:64173
KEY="your-bridge-api-key-or-empty"
AUTH=""
[ -n "$KEY" ] && AUTH="-H Authorization: Bearer $KEY"

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
| `401 Unauthorized` | Set `Authorization: Bearer <BRIDGE_API_KEY>` in your client, or disable the API key in `.env`. |
| Models list is empty | The bridge could not refresh the Anthropic token. Check `bridge.log` and verify the credential files are valid. |
| Service fails to start | Run the bridge manually to see the error: `source .env && .venv/bin/python3 server.py` |
| `OAuth refresh error: invalid_grant` | Your refresh token was revoked. Re-run `claude` to log in again, then `opencode auth login`. |
| `Provider already exists` in `add-to-hermes.sh` / `add-to-openclaw.sh` | The script protects your config from accidental overwrites. | Run the script with `--force` or choose a different provider name. |
| `Hermes config is not valid YAML` | The script detected a YAML syntax problem after editing. | A backup was created; restore it and report the issue. |
| `OpenClaw config is not valid JSON` | The script detected a JSON syntax problem after editing. | A backup was created; restore it and report the issue. |

---

## ⚠️ Disclaimer

This is an **unofficial, experimental** project. The author is not affiliated with Anthropic, Claude Code, Claude, OpenCode, or any other mentioned service.

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
