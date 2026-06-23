#!/usr/bin/env bash
#
# Add the local Anthropic OAuth Bridge as a named custom OpenAI-compatible provider in Hermes.
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

HERMES_CONFIG="${HERMES_HOME:-${HOME}/.hermes}/config.yaml"
BASE_URL="http://127.0.0.1:${PORT}/v1"

echo ""
echo -e "${BOLD}Hermes provider configuration${RESET}"
echo "────────────────────────────────────────────────────────────────"
echo "  Provider: ${PROVIDER_NAME}"
echo "  Base URL: ${BASE_URL}"
echo "  Model:    ${DEFAULT_MODEL}"
echo "  Config:   ${HERMES_CONFIG}"
echo ""

# ---------------------------------------------------------------------------
# Read and modify config
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$HERMES_CONFIG")"

if [[ -f "$HERMES_CONFIG" ]]; then
    cp "$HERMES_CONFIG" "${HERMES_CONFIG}.backup.$(date +%s)"
    success "Created backup of existing Hermes config."
fi

"$PYTHON_BIN" - <<PY
import os
import sys
import yaml

config_path = os.path.expanduser("${HERMES_CONFIG}")
provider_name = "${PROVIDER_NAME}"
base_url = "${BASE_URL}"
api_key = """${BRIDGE_API_KEY:-}""".strip()
model = "${DEFAULT_MODEL}"
force = "${FORCE}".lower() in ("true", "1", "yes")

try:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
except Exception as e:
    print(f"Error reading Hermes config: {e}", file=sys.stderr)
    sys.exit(1)

custom_providers = data.setdefault("custom_providers", [])

# Check for existing provider
existing = [p for p in custom_providers if p.get("name") == provider_name]
if existing and not force:
    print(f"Provider '{provider_name}' already exists in Hermes config.")
    print("Use --force to overwrite, or choose a different provider name.")
    sys.exit(2)

# Remove existing entry with same name
custom_providers[:] = [p for p in custom_providers if p.get("name") != provider_name]

provider = {
    "name": provider_name,
    "base_url": base_url,
    "api_mode": "chat_completions",
    "models": [{"id": model, "name": model}],
}
if api_key:
    provider["api_key"] = api_key

custom_providers.append(provider)

model_section = data.setdefault("model", {})
model_section["provider"] = f"custom:{provider_name}"
model_section["default"] = model

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

print(f"Updated {config_path}")
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
success "Hermes is now configured with provider 'custom:${PROVIDER_NAME}'."
echo ""
echo "Start Hermes and switch models with:"
echo "  /model custom:${PROVIDER_NAME}:${DEFAULT_MODEL}"
echo ""
