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
# Argument parsing
# ---------------------------------------------------------------------------
FORCE=false
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--force)
            FORCE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [model] [provider-name] [-f|--force]"
            echo "  model          Default model to use (default: claude-sonnet-4-5)"
            echo "  provider-name  Name of the custom provider (default: anthropic-oauth-bridge)"
            echo "  -f, --force    Overwrite an existing provider entry without asking"
            exit 0
            ;;
        -*)
            error "Unknown option: $1"
            exit 1
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

set -- "${POSITIONAL[@]}"

DEFAULT_MODEL="${1:-claude-sonnet-4-5}"
PROVIDER_NAME="${2:-anthropic-oauth-bridge}"

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

# ---------------------------------------------------------------------------
# Read and modify config
# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR"

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
force = "${FORCE}".lower() in ("true", "1", "yes")

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

if provider_name in providers and not force:
    print(f"Provider '{provider_name}' already exists in OpenClaw config.")
    print("Use --force to overwrite, or choose a different provider name.")
    sys.exit(2)

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
if [[ $status -eq 2 ]]; then
    echo ""
    warn "Provider '${PROVIDER_NAME}' already exists. Run with --force to overwrite."
    exit 0
elif [[ $status -ne 0 ]]; then
    exit $status
fi

echo ""
success "OpenClaw provider '${PROVIDER_NAME}' is configured."
echo ""
echo "Apply the config and restart the gateway:"
echo "  openclaw gateway config.apply --file ${CONFIG_PATH}"
echo ""
echo "Then select the model in chat:"
echo "  /model ${DEFAULT_MODEL}"
echo ""
