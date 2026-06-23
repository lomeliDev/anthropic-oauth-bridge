#!/usr/bin/env bash
#
# Add the local Anthropic OAuth Bridge as a named OpenAI-compatible provider in Hermes.
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

PYTHON_BIN="${REPO_DIR}/.venv/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    error "Python venv not found at ${REPO_DIR}/.venv. Run ./install.sh first."
    exit 1
fi

if ! "$PYTHON_BIN" -c "import yaml" 2>/dev/null; then
    info "Installing PyYAML into the bridge venv ..."
    "${REPO_DIR}/.venv/bin/pip" install -q pyyaml || {
        error "Failed to install PyYAML."
        exit 1
    }
fi

if ! command -v hermes >/dev/null 2>&1; then
    error "Hermes CLI was not found. Install Hermes first, then run this script again."
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure the bridge has an API key. If .env does not have one, generate it
# automatically so clients like Hermes can authenticate.
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

BASE_URL="http://127.0.0.1:${PORT}/v1"

echo ""
echo -e "${BOLD}Hermes provider configuration${RESET}"
echo "────────────────────────────────────────────────────────────────"
echo "  Provider: ${PROVIDER_NAME}"
echo "  Base URL: ${BASE_URL}"
echo "  Model:    ${DEFAULT_MODEL}"
echo "  Config:   ${HERMES_CONFIG:-${HOME}/.hermes/config.yaml}"
echo ""

HERMES_CONFIG="${HERMES_HOME:-${HOME}/.hermes}/config.yaml"
mkdir -p "$(dirname "$HERMES_CONFIG")"

# ---------------------------------------------------------------------------
# Backup existing config
# ---------------------------------------------------------------------------
if [[ -f "$HERMES_CONFIG" ]]; then
    cp "$HERMES_CONFIG" "${HERMES_CONFIG}.backup.$(date +%s)"
    success "Created backup of existing Hermes config."
fi

# ---------------------------------------------------------------------------
# Ask whether to set as active provider
# ---------------------------------------------------------------------------
read -rp "Set '${PROVIDER_NAME}' as the active Hermes provider? [Y/n]: " SET_ACTIVE
SET_ACTIVE="${SET_ACTIVE:-Y}"

# ---------------------------------------------------------------------------
# Read and modify config
# ---------------------------------------------------------------------------
"$PYTHON_BIN" - <<PY
import os
import sys
import yaml

config_path = os.path.expanduser("${HERMES_CONFIG}")
provider_name = "${PROVIDER_NAME}"
base_url = "${BASE_URL}"
api_key = """${BRIDGE_API_KEY:-}""".strip()
model = "${DEFAULT_MODEL}"
set_active = "${SET_ACTIVE}".lower().startswith("y")

try:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
except Exception as e:
    print(f"Error reading Hermes config: {e}", file=sys.stderr)
    sys.exit(1)

# Hermes resolves named providers from the top-level 'providers' map.
providers = data.setdefault("providers", {})
provider = {
    "base_url": base_url,
    "api_mode": "chat_completions",
    "models": [{"id": model, "name": model}],
}
if api_key:
    provider["api_key"] = api_key
providers[provider_name] = provider

# Keep custom_providers in sync for older Hermes versions.
custom_providers = data.setdefault("custom_providers", [])
custom_providers[:] = [p for p in custom_providers if p.get("name") != provider_name]
custom_providers.append({
    "name": provider_name,
    "base_url": base_url,
    "api_mode": "chat_completions",
    "models": [{"id": model, "name": model}],
    **({"api_key": api_key} if api_key else {}),
})

model_section = data.setdefault("model", {})
if set_active:
    model_section["provider"] = provider_name
    model_section["default"] = model

# If the active provider points to our named provider, remove any stale
# global custom base_url/api_key. Hermes sometimes auto-fills OpenRouter's
# URL here when the provider name is 'custom' or the model is not found.
if model_section.get("provider") == provider_name:
    removed = []
    for key in ("base_url", "api_key"):
        if key in model_section:
            del model_section[key]
            removed.append(key)
    if removed:
        print(f"Removed stale model.{', '.join(removed)} so provider '{provider_name}' is used")

try:
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
except Exception as e:
    print(f"Error writing Hermes config: {e}", file=sys.stderr)
    sys.exit(1)

# Validate written YAML
try:
    with open(config_path, "r") as f:
        yaml.safe_load(f)
except Exception as e:
    print(f"Generated config is not valid YAML: {e}", file=sys.stderr)
    sys.exit(1)

# Verify the final config and warn about common misconfigurations.
final_provider = model_section.get("provider")
final_base_url = model_section.get("base_url", "")
if final_provider != provider_name:
    print(f"WARNING: model.provider is '{final_provider}', not '{provider_name}'.")
if "openrouter" in str(final_base_url).lower():
    print(f"WARNING: model.base_url still points to OpenRouter ({final_base_url}).")
    print("         Remove it manually or the bridge will not be used.")

print(f"Updated {config_path}")
PY

status=$?
if [[ $status -ne 0 ]]; then
    exit $status
fi

echo ""
if [[ "${SET_ACTIVE}" =~ ^[Yy] ]]; then
    success "Hermes is configured to use '${PROVIDER_NAME}' as the active provider."
    echo "  Model: ${DEFAULT_MODEL}"
else
    success "Hermes now knows the provider '${PROVIDER_NAME}'."
    echo ""
    echo "To activate it manually, run:"
    echo "  /model ${PROVIDER_NAME}:${DEFAULT_MODEL}"
fi

# ---------------------------------------------------------------------------
# Restart Hermes gateway
# ---------------------------------------------------------------------------
echo ""
read -rp "Restart Hermes gateway now? [Y/n]: " RESTART_ANSWER
RESTART_ANSWER="${RESTART_ANSWER:-Y}"
if [[ "$RESTART_ANSWER" =~ ^[Yy]$ ]]; then
    echo ""
    info "Hermes gateway status BEFORE restart:"
    systemctl status hermes-gateway --no-pager || true

    echo ""
    info "Restarting Hermes gateway ..."
    if systemctl restart hermes-gateway 2>/dev/null; then
        success "Hermes gateway restarted."
    else
        warn "systemctl restart failed, trying 'hermes gateway restart' ..."
        hermes gateway restart || true
    fi

    echo ""
    info "Hermes gateway status AFTER restart:"
    systemctl status hermes-gateway --no-pager || true
else
    info "Skipped Hermes gateway restart. Remember to restart it manually:"
    echo "  systemctl restart hermes-gateway"
    echo "  # or"
    echo "  hermes gateway restart"
fi

echo ""
