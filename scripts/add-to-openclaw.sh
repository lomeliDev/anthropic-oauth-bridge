#!/usr/bin/env bash
#
# Add the local Anthropic OAuth Bridge as a custom OpenAI-compatible provider in OpenClaw.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# Pretty output helpers
# ---------------------------------------------------------------------------
RESET='\033[0m'
BOLD='\033[1m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'

info()    { echo -e "${CYAN}ℹ${RESET}  $*"; }
success() { echo -e "${GREEN}✔${RESET}  $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✖${RESET}  $*"; }

# ---------------------------------------------------------------------------
# Pre-flight validations
# ---------------------------------------------------------------------------
if [[ ! -f ".env" ]]; then
    error ".env not found. Run ./install.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source .env

if [[ -z "${PORT:-}" ]]; then
    error "PORT is not set in .env. Run ./install.sh first."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    error "python3 is required to edit OpenClaw config."
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure the bridge has an API key. If .env does not have one, generate it
# automatically so clients like OpenClaw can authenticate.
# ---------------------------------------------------------------------------
if [[ -z "${BRIDGE_API_KEY:-}" ]]; then
    warn "No BRIDGE_API_KEY found in .env. Generating one ..."
    NEW_KEY=$(openssl rand -hex 24 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(24))")
    echo "BRIDGE_API_KEY=${NEW_KEY}" >> .env
    chmod 600 .env
    BRIDGE_API_KEY="${NEW_KEY}"
    success "API key saved to .env."

    info "Restarting bridge service to pick up the new API key ..."
    if systemctl restart anthropic-oauth-bridge 2>/dev/null; then
        success "Bridge service restarted."
    else
        warn "Could not restart anthropic-oauth-bridge via systemctl."
        warn "If the bridge is already running, you may need to restart it manually."
    fi
    sleep 2
fi

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------
ask_with_default() {
    local prompt="$1"
    local default_value="$2"
    local input
    read -rp "${prompt} [${default_value}]: " input
    echo "${input:-$default_value}"
}

if [[ $# -ge 1 ]]; then
    DEFAULT_MODEL="$1"
else
    DEFAULT_MODEL=$(ask_with_default "Model id" "claude-sonnet-4-5")
fi

if [[ $# -ge 2 ]]; then
    PROVIDER_NAME="$2"
else
    PROVIDER_NAME=$(ask_with_default "Provider name" "anthropic-oauth-bridge")
fi

CONFIG_DIR="${HOME}/.openclaw"
BASE_URL="http://127.0.0.1:${PORT}/v1"

CONFIG_PATH=""
for candidate in "${CONFIG_DIR}/openclaw.json" "${CONFIG_DIR}/config.json"; do
    if [[ -f "$candidate" ]]; then
        CONFIG_PATH="$candidate"
        break
    fi
done

if [[ -z "$CONFIG_PATH" ]]; then
    CONFIG_PATH="${CONFIG_DIR}/openclaw.json"
fi

echo ""
echo -e "${BOLD}OpenClaw provider configuration${RESET}"
echo "────────────────────────────────────────────────────────────────"
echo "  Provider: ${PROVIDER_NAME}"
echo "  Config:   ${CONFIG_PATH}"
echo "  Base URL: ${BASE_URL}"
echo "  Model:    ${DEFAULT_MODEL}"
echo ""

mkdir -p "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# Read and modify config
# ---------------------------------------------------------------------------
python3 - <<PY
import json
import os
import sys
import time

path = os.path.expanduser("${CONFIG_PATH}")
provider_name = "${PROVIDER_NAME}"
base_url = "${BASE_URL}"
api_key = """${BRIDGE_API_KEY:-}""".strip()
model = "${DEFAULT_MODEL}"

try:
    data = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
except Exception as e:
    print(f"Error reading OpenClaw config: {e}", file=sys.stderr)
    sys.exit(1)

models = data.setdefault("models", {})
models.setdefault("mode", "merge")
providers = models.setdefault("providers", {})

if provider_name in providers:
    print(f"Provider '{provider_name}' already exists; overwriting.")

provider = {
    "baseUrl": base_url,
    "api": "openai-completions",
    "models": [
        {
            "id": model,
            "name": model,
            "reasoning": False,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 64000,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }
    ],
}
if api_key:
    provider["apiKey"] = api_key
providers[provider_name] = provider

agents = data.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
agent_models = defaults.setdefault("models", {})
agent_models[f"{provider_name}/{model}"] = {"alias": model}

try:
    backup = path + ".backup." + str(int(time.time()))
    if os.path.exists(path):
        os.replace(path, backup)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
except Exception as e:
    print(f"Error writing OpenClaw config: {e}", file=sys.stderr)
    sys.exit(1)

# Validate written JSON
try:
    with open(path, "r") as f:
        json.load(f)
except Exception as e:
    print(f"Generated config is not valid JSON: {e}", file=sys.stderr)
    sys.exit(1)

print("Updated", path)
PY

status=$?
if [[ $status -ne 0 ]]; then
    exit $status
fi

echo ""
success "OpenClaw provider '${PROVIDER_NAME}' is configured."
echo ""

# ---------------------------------------------------------------------------
# Apply config and restart OpenClaw gateway
# ---------------------------------------------------------------------------
echo ""
read -rp "Apply OpenClaw config and restart the gateway now? [Y/n]: " RESTART_ANSWER
RESTART_ANSWER="${RESTART_ANSWER:-Y}"
if [[ "$RESTART_ANSWER" =~ ^[Yy]$ ]]; then
    echo ""
    info "OpenClaw gateway status BEFORE restart:"
    systemctl status openclaw-gateway --no-pager 2>/dev/null || true

    echo ""
    info "Applying OpenClaw config ..."
    openclaw gateway config.apply --file "${CONFIG_PATH}" || true

    echo ""
    info "Restarting OpenClaw gateway ..."
    if systemctl restart openclaw-gateway 2>/dev/null; then
        success "OpenClaw gateway restarted."
    else
        warn "Could not restart 'openclaw-gateway' via systemctl."
        warn "Please restart it manually with your OpenClaw management command."
    fi

    echo ""
    info "OpenClaw gateway status AFTER restart:"
    systemctl status openclaw-gateway --no-pager 2>/dev/null || true
else
    info "Skipped OpenClaw gateway restart. Apply the config manually with:"
    echo "  openclaw gateway config.apply --file ${CONFIG_PATH}"
fi

echo ""
echo "Then select the model in chat:"
echo "  /model ${DEFAULT_MODEL}"
echo ""
