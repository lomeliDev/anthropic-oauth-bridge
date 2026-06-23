#!/usr/bin/env bash
#
# Anthropic OAuth -> OpenAI Bridge installer
# Supports: Linux (systemd), macOS (launchd), and a portable fallback script.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

print_header() {
    echo ""
    echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════════${RESET}"
    echo -e "${CYAN}${BOLD}  Anthropic OAuth Bridge installer${RESET}"
    echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════════${RESET}"
    echo ""
}

info()    { echo -e "${CYAN}ℹ${RESET}  $*"; }
success() { echo -e "${GREEN}✔${RESET}  $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✖${RESET}  $*"; }

# ---------------------------------------------------------------------------
# Defaults and paths
# ---------------------------------------------------------------------------
OPENCODE_CONFIG="${HOME}/.config/opencode/opencode.jsonc"
DEFAULT_PORT=64173
DEFAULT_ANTHROPIC_AUTH="${ANTHROPIC_AUTH_PATH:-$HOME/.local/share/opencode/auth.json}"
DEFAULT_CLAUDE_CREDENTIALS="${CLAUDE_CREDENTIALS_PATH:-$HOME/.claude/.credentials.json}"
DEFAULT_ANTHROPIC_CLIENT_ID="${ANTHROPIC_CLIENT_ID:-9d1c250a-e61b-44d9-88ed-5944d1962f5e}"

export PATH="${HOME}/.opencode/bin:${HOME}/.local/bin:${HOME}/.nvm/versions/node/current/bin:${HOME}/.nvm/current/bin:${PATH}"

print_step() {
    local num="$1"
    local title="$2"
    echo ""
    echo -e "${CYAN}${BOLD}Step ${num}: ${title}${RESET}"
    echo "────────────────────────────────────────────────────────────────"
}

print_prereq_help() {
    echo ""
    echo -e "${BOLD}Manual prerequisite steps${RESET}"
    echo "────────────────────────────────────────────────────────────────"
    echo "The bridge reuses the Anthropic OAuth session created by Claude Code + OpenCode."
    echo ""
    echo "1. Install the OpenCode CLI:"
    echo "     curl -fsSL https://opencode.ai/install | bash"
    echo ""
    echo "2. Install the Claude Code CLI:"
    echo "     npm install -g @anthropic-ai/claude-code"
    echo ""
    echo "3. Log in with Claude Code:"
    echo "     claude"
    echo ""
    echo "4. Add the Anthropic auth plugin to ${OPENCODE_CONFIG}:"
    echo ""
    echo '     {'
    echo '       "plugin": ["opencode-claude-auth@latest"]'
    echo '     }'
    echo ""
    echo "5. Authenticate with Anthropic through OpenCode:"
    echo "     opencode auth login"
    echo ""
    echo "   Select  Anthropic  and sign in with the same account you used for claude."
    echo ""
    echo "6. Verify the credential was stored:"
    echo "     opencode auth list"
    echo ""
    echo "Then re-run this installer:"
    echo "     ./install.sh"
    echo ""
}

# ---------------------------------------------------------------------------
# OpenCode prerequisite helpers
# ---------------------------------------------------------------------------
install_opencode() {
    warn "OpenCode CLI not found."
    read -rp "Install OpenCode automatically? [Y/n]: " INSTALL_OPENCODE
    INSTALL_OPENCODE="${INSTALL_OPENCODE:-Y}"
    if [[ ! "$INSTALL_OPENCODE" =~ ^[Yy]$ ]]; then
        print_prereq_help
        exit 1
    fi
    info "Installing OpenCode ..."
    curl -fsSL https://opencode.ai/install | bash
    export PATH="${HOME}/.opencode/bin:${HOME}/.local/bin:${PATH}"
    if ! command -v opencode >/dev/null 2>&1; then
        error "OpenCode was installed but is not on PATH in this shell."
        error "Run this command in a new terminal, then re-run the installer:"
        error "  export PATH=\"${HOME}/.opencode/bin:\${HOME}/.local/bin:\${PATH}\""
        exit 1
    fi
    success "OpenCode installed."
}

install_node_via_nvm() {
    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
        return 0
    fi
    info "Node.js / npm not found. Installing via nvm ..."
    if [[ ! -d "$HOME/.nvm" ]]; then
        curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
    fi
    export NVM_DIR="$HOME/.nvm"
    # shellcheck disable=SC1091
    [[ -s "$NVM_DIR/nvm.sh" ]] && \. "$NVM_DIR/nvm.sh"
    nvm install 20
    nvm use 20
    nvm alias default 20
    export PATH="$NVM_DIR/versions/node/$(nvm current)/bin:$PATH"
    if ! command -v npm >/dev/null 2>&1; then
        error "npm installation via nvm failed."
        return 1
    fi
    success "Node.js / npm installed."
}

install_claude_cli() {
    warn "Claude Code CLI (claude) not found."
    read -rp "Install Claude Code CLI automatically? [Y/n]: " INSTALL_CLAUDE
    INSTALL_CLAUDE="${INSTALL_CLAUDE:-Y}"
    if [[ ! "$INSTALL_CLAUDE" =~ ^[Yy]$ ]]; then
        print_prereq_help
        exit 1
    fi

    if ! command -v npm >/dev/null 2>&1; then
        install_node_via_nvm || {
            error "Could not install npm automatically."
            print_prereq_help
            exit 1
        }
    fi

    info "Installing Claude Code CLI via npm ..."
    npm install -g @anthropic-ai/claude-code
    export PATH="${HOME}/.local/bin:${PATH}"
    if ! command -v claude >/dev/null 2>&1; then
        error "Claude Code CLI was installed but is not on PATH in this shell."
        error "Try:"
        error "  export PATH=\"${HOME}/.local/bin:\${HOME}/.nvm/versions/node/current/bin:\${PATH}\""
        exit 1
    fi
    success "Claude Code CLI installed."
}

claude_session_exists() {
    # Linux / Windows credentials file
    if [[ -f "$DEFAULT_CLAUDE_CREDENTIALS" ]]; then
        python3 - <<PY
import json, sys
try:
    with open("$DEFAULT_CLAUDE_CREDENTIALS") as f:
        d = json.load(f)
    if (d.get("claudeAiOauth") or {}).get("accessToken"):
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
        return
    fi
    # macOS keychain fallback
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if security find-generic-password -s "Claude Code-credentials" -w >/dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

opencode_anthropic_auth_exists() {
    local auth_json="$DEFAULT_ANTHROPIC_AUTH"
    [[ -f "$auth_json" ]] || return 1
    python3 - <<PY
import json, sys
try:
    with open("$auth_json") as f:
        data = json.load(f)
    entry = data.get("anthropic")
    if isinstance(entry, dict) and entry.get("access"):
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
}

run_interactive_login() {
    local tool="$1"
    local cmd="$2"
    echo ""
    warn "A browser tab will open so you can authenticate with Anthropic."
    warn "Do NOT close this terminal until the login finishes and you return here."
    info "The installer will now run: ${cmd}"
    read -rp "Press Enter to open the login page ..."
    $cmd || true
}

install_opencode_plugin() {
    local config_path="$1"
    mkdir -p "$(dirname "$config_path")"
    python3 - <<PY
import json, re, sys, os

path = os.path.expanduser("$config_path")

def strip_jsonc_comments(src):
    # Remove // comments
    src = re.sub(r'//[^\n]*', '', src)
    # Remove /* */ comments (non-greedy)
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    return src

if os.path.exists(path):
    with open(path, "r") as f:
        raw = f.read()
    try:
        data = json.loads(strip_jsonc_comments(raw))
    except Exception:
        data = {}
else:
    data = {}

plugins = data.get("plugin", [])
if not isinstance(plugins, list):
    plugins = [plugins]

if not any("opencode-claude-auth" in p for p in plugins):
    plugins.append("opencode-claude-auth@latest")
    data["plugin"] = plugins
    # Preserve any $schema key if present
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print("Plugin added.")
else:
    print("Plugin already present.")
PY
}

plugin_is_configured() {
    if [[ ! -f "$OPENCODE_CONFIG" ]]; then
        return 1
    fi
    python3 - <<PY
import json, re, sys
path = "$OPENCODE_CONFIG"
try:
    with open(path, "r") as f:
        raw = f.read()
    raw = re.sub(r'//[^\n]*', '', raw)
    raw = re.sub(r'/\*.*?\*/', '', raw, flags=re.DOTALL)
    data = json.loads(raw)
    plugins = data.get("plugin", [])
    if not isinstance(plugins, list):
        plugins = [plugins]
    if any("opencode-claude-auth" in p for p in plugins):
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
}

check_opencode_prerequisites() {
    # Step 1: OpenCode CLI
    print_step 1 "Install OpenCode CLI"
    if ! command -v opencode >/dev/null 2>&1; then
        install_opencode
    else
        success "OpenCode CLI found: $(opencode --version 2>/dev/null | head -n1 || echo opencode)"
    fi

    # Step 2: Claude Code CLI
    print_step 2 "Install Claude Code CLI (claude)"
    if ! command -v claude >/dev/null 2>&1; then
        install_claude_cli
    else
        success "Claude Code CLI found: $(claude --version 2>/dev/null | head -n1 || echo claude)"
    fi

    # Step 3: Claude Code login
    print_step 3 "Log in with Claude Code (claude)"
    if claude_session_exists; then
        success "A Claude Code session was detected."
    else
        warn "No Claude Code session was detected. The installer can run 'claude' for you now."
        warn "If you skip this, the installer will exit and you can re-run it later."
        read -rp "Run 'claude' now? [Y/n]: " RUN_CLAUDE_LOGIN
        RUN_CLAUDE_LOGIN="${RUN_CLAUDE_LOGIN:-Y}"
        if [[ "$RUN_CLAUDE_LOGIN" =~ ^[Yy]$ ]]; then
            run_interactive_login "claude" "claude"
        fi
        if ! claude_session_exists; then
            warn "Still no Claude Code session detected."
            read -rp "Did you complete the Claude login successfully? [y/N]: " CLAUDE_OK
            if [[ ! "${CLAUDE_OK:-N}" =~ ^[Yy]$ ]]; then
                print_prereq_help
                exit 1
            fi
        fi
    fi
    success "Claude Code login verified."

    # Step 4: opencode-claude-auth plugin
    print_step 4 "Configure opencode-claude-auth plugin"
    if plugin_is_configured; then
        success "opencode-claude-auth plugin is already configured."
    else
        warn "The opencode-claude-auth plugin is not configured."
        info "It is required so OpenCode can authenticate with Anthropic via Claude Code OAuth."
        read -rp "Add the plugin automatically? [Y/n]: " INSTALL_PLUGIN
        INSTALL_PLUGIN="${INSTALL_PLUGIN:-Y}"
        if [[ "$INSTALL_PLUGIN" =~ ^[Yy]$ ]]; then
            install_opencode_plugin "$OPENCODE_CONFIG"
        else
            print_prereq_help
            exit 1
        fi
    fi

    # Step 5: OpenCode Anthropic OAuth login
    print_step 5 "Log in with OpenCode (Anthropic OAuth)"
    if opencode_anthropic_auth_exists; then
        success "OpenCode Anthropic OAuth credential found."
    else
        warn "No OpenCode Anthropic OAuth credential found. The installer can run 'opencode auth login' for you now."
        warn "If you skip this, the installer will exit and you can re-run it later."
        read -rp "Run 'opencode auth login' now? [Y/n]: " RUN_OPENCODE_LOGIN
        RUN_OPENCODE_LOGIN="${RUN_OPENCODE_LOGIN:-Y}"
        if [[ "$RUN_OPENCODE_LOGIN" =~ ^[Yy]$ ]]; then
            run_interactive_login "opencode" "opencode auth login"
        fi
        if ! opencode_anthropic_auth_exists; then
            warn "Still no OpenCode Anthropic OAuth credential found."
            read -rp "Did you complete the OpenCode login successfully? [y/N]: " OPENCODE_OK
            if [[ ! "${OPENCODE_OK:-N}" =~ ^[Yy]$ ]]; then
                print_prereq_help
                exit 1
            fi
        fi
    fi
    success "OpenCode Anthropic OAuth login verified."

    # Step 6: Credential file sanity check
    print_step 6 "Validate credential files"
    local cred_files_ok=true
    for path in "$DEFAULT_ANTHROPIC_AUTH" "$DEFAULT_CLAUDE_CREDENTIALS"; do
        if [[ -f "$path" ]]; then
            success "Found ${path}"
        else
            warn "Missing ${path}"
            cred_files_ok=false
        fi
    done
    if [[ "$cred_files_ok" == "false" ]]; then
        error "Some credential files are still missing."
        info "Try running: opencode auth list"
        info "If the login succeeded but files are missing, the plugin may use different paths."
        info "You can override paths with ANTHROPIC_AUTH_PATH and CLAUDE_CREDENTIALS_PATH."
        read -rp "Continue anyway? [y/N]: " CONTINUE
        if [[ ! "${CONTINUE:-N}" =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
DETECTED_OS="unknown"
DETECTED_INIT="none"

if [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$OSTYPE" == "linux"* ]]; then
    DETECTED_OS="linux"
    if command -v systemctl >/dev/null 2>&1; then
        DETECTED_INIT="systemd"
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    DETECTED_OS="macos"
    if command -v launchctl >/dev/null 2>&1; then
        DETECTED_INIT="launchd"
    fi
fi

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
print_header

if [[ "$EUID" -eq 0 ]]; then
    warn "You are running this installer as root."
    warn "The bridge and the service will be configured for the root user."
    read -rp "Continue as root? [y/N]: " CONTINUE_ROOT
    if [[ ! "${CONTINUE_ROOT:-N}" =~ ^[Yy]$ ]]; then
        info "Please run the installer as your normal user and try again."
        exit 0
    fi
fi

echo ""
echo -e "${BOLD}What this script will do${RESET}"
echo "────────────────────────────────────────────────────────────────"
echo "  1. Install OpenCode and the Claude Code CLI if they are missing."
echo "  2. Run the OAuth logins for you (browser tabs will open)."
echo "  3. Install the bridge and its Python dependencies."
echo "  4. Ask for a port and an optional API key."
echo "  5. Install and start a system service (systemd / launchd)."
echo ""
echo -e "${YELLOW}This is an unofficial tool. Use it at your own risk.${RESET}"
echo -e "${YELLOW}The author is not responsible for bans, rate limits or any other consequences.${RESET}"
echo ""
echo -e "${CYAN}Just press Enter to accept the defaults when prompted.${RESET}"
echo ""

# ---------------------------------------------------------------------------
# Python check (needed early for the prerequisite helpers)
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    error "python3 was not found. Please install Python 3.9 or newer and try again."
    exit 1
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 9 ]]; }; then
    error "Python 3.9+ is required. Found ${PY_MAJOR}.${PY_MINOR}."
    exit 1
fi
success "Python ${PY_MAJOR}.${PY_MINOR} is ready."

check_opencode_prerequisites

info "Detected platform: ${DETECTED_OS} (${DETECTED_INIT})"

# ---------------------------------------------------------------------------
# Virtual environment & dependencies
# ---------------------------------------------------------------------------
if [[ ! -d ".venv" ]]; then
    info "Creating Python virtual environment in .venv ..."
    python3 -m venv .venv
fi

info "Installing Python dependencies ..."
.venv/bin/pip install -q -r requirements.txt
success "Dependencies installed."

# ---------------------------------------------------------------------------
# Configuration prompts
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}Configuration${RESET}"
echo "────────────────────────────────────────────────────────────────"

read -rp "Listen port [${DEFAULT_PORT}]: " PORT
PORT="${PORT:-$DEFAULT_PORT}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [[ "$PORT" -lt 1 ]] || [[ "$PORT" -gt 65535 ]]; then
    error "Invalid port: ${PORT}. Please enter a number between 1 and 65535."
    exit 1
fi

RANDOM_KEY="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"

echo ""
echo "You can protect the bridge with an API key."
echo "Clients must send: Authorization: Bearer <key>"
read -rp "Require an API key? [Y/n]: " NEED_KEY
NEED_KEY="${NEED_KEY:-Y}"

API_KEY=""
if [[ "$NEED_KEY" =~ ^[Yy]$ ]]; then
    read -rp "Bridge API key [random]: " API_KEY
    API_KEY="${API_KEY:-$RANDOM_KEY}"
    if [[ -z "$API_KEY" ]]; then
        error "API key cannot be empty when authentication is enabled."
        exit 1
    fi
    success "API key set."
else
    info "Running without client API key authentication."
fi

# ---------------------------------------------------------------------------
# Write .env
# ---------------------------------------------------------------------------
cat > .env <<EOF
PORT=${PORT}
ANTHROPIC_AUTH_PATH=${DEFAULT_ANTHROPIC_AUTH}
CLAUDE_CREDENTIALS_PATH=${DEFAULT_CLAUDE_CREDENTIALS}
ANTHROPIC_CLIENT_ID=${DEFAULT_ANTHROPIC_CLIENT_ID}
EOF

if [[ -n "$API_KEY" ]]; then
    echo "BRIDGE_API_KEY=${API_KEY}" >> .env
fi
chmod 600 .env
success "Wrote configuration to .env"

# ---------------------------------------------------------------------------
# Daemon configuration generators
# ---------------------------------------------------------------------------
DAEMON_DIR="${REPO_DIR}/daemon"
mkdir -p "$DAEMON_DIR"

PYTHON_BIN_DIR="${REPO_DIR}/.venv/bin"

info "Generating daemon files ..."

# systemd service
sed -e "s|%USER%|$(whoami)|g" \
    -e "s|%WORK_DIR%|${REPO_DIR}|g" \
    -e "s|%PYTHON_BIN_DIR%|${PYTHON_BIN_DIR}|g" \
    -e "s|%PORT%|${PORT}|g" \
    -e "s|%BRIDGE_API_KEY%|${API_KEY}|g" \
    -e "s|%ANTHROPIC_AUTH_PATH%|${DEFAULT_ANTHROPIC_AUTH}|g" \
    -e "s|%CLAUDE_CREDENTIALS_PATH%|${DEFAULT_CLAUDE_CREDENTIALS}|g" \
    -e "s|%ANTHROPIC_CLIENT_ID%|${DEFAULT_ANTHROPIC_CLIENT_ID}|g" \
    "${REPO_DIR}/anthropic-oauth-bridge.service" > "${DAEMON_DIR}/anthropic-oauth-bridge.service"

# launchd plist
cat > "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lomelidev.anthropic-oauth-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN_DIR}/python3</string>
        <string>${REPO_DIR}/server.py</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${PYTHON_BIN_DIR}:/usr/local/bin:/usr/bin:/bin</string>
        <key>PORT</key>
        <string>${PORT}</string>
        <key>ANTHROPIC_AUTH_PATH</key>
        <string>${DEFAULT_ANTHROPIC_AUTH}</string>
        <key>CLAUDE_CREDENTIALS_PATH</key>
        <string>${DEFAULT_CLAUDE_CREDENTIALS}</string>
        <key>ANTHROPIC_CLIENT_ID</key>
        <string>${DEFAULT_ANTHROPIC_CLIENT_ID}</string>
EOF

if [[ -n "$API_KEY" ]]; then
    cat >> "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" <<EOF
        <key>BRIDGE_API_KEY</key>
        <string>${API_KEY}</string>
EOF
fi

cat >> "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" <<EOF
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${REPO_DIR}/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_DIR}/bridge.log</string>
</dict>
</plist>
EOF

# Portable fallback run script
cat > "${DAEMON_DIR}/run.sh" <<EOF
#!/usr/bin/env bash
# Manual runner used when systemd/launchd are not available.
set -euo pipefail
cd "${REPO_DIR}"
source .env
exec ${PYTHON_BIN_DIR}/python3 server.py --host 0.0.0.0 --port ${PORT}
EOF
chmod +x "${DAEMON_DIR}/run.sh"

success "Daemon files written to ${DAEMON_DIR}/"

# ---------------------------------------------------------------------------
# Install & start daemon
# ---------------------------------------------------------------------------
install_systemd() {
    local unit="/etc/systemd/system/anthropic-oauth-bridge.service"
    info "Installing systemd service ..."
    if [[ "$EUID" -eq 0 ]]; then
        cp "${DAEMON_DIR}/anthropic-oauth-bridge.service" "$unit"
        systemctl daemon-reload
        systemctl enable --now anthropic-oauth-bridge
    elif command -v sudo >/dev/null 2>&1; then
        sudo cp "${DAEMON_DIR}/anthropic-oauth-bridge.service" "$unit"
        sudo systemctl daemon-reload
        sudo systemctl enable --now anthropic-oauth-bridge
    else
        error "Root privileges are required to install the systemd service."
        return 1
    fi
    success "systemd service installed and started."
}

install_launchd() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist="${plist_dir}/com.lomelidev.anthropic-oauth-bridge.plist"
    mkdir -p "$plist_dir"
    info "Installing launchd agent ..."
    cp "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" "$plist"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load -w "$plist"
    success "launchd agent installed and started."
}

echo ""
echo -e "${BOLD}Daemon installation${RESET}"
echo "────────────────────────────────────────────────────────────────"

if [[ "$DETECTED_INIT" == "systemd" ]]; then
    read -rp "Install and start the systemd service? [Y/n]: " INSTALL_DAEMON
    INSTALL_DAEMON="${INSTALL_DAEMON:-Y}"
    if [[ "$INSTALL_DAEMON" =~ ^[Yy]$ ]]; then
        install_systemd || exit 1
    else
        info "Skipped systemd install. Service file is available at:"
        info "  ${DAEMON_DIR}/anthropic-oauth-bridge.service"
    fi
elif [[ "$DETECTED_INIT" == "launchd" ]]; then
    read -rp "Install and start the launchd agent? [Y/n]: " INSTALL_DAEMON
    INSTALL_DAEMON="${INSTALL_DAEMON:-Y}"
    if [[ "$INSTALL_DAEMON" =~ ^[Yy]$ ]]; then
        install_launchd || exit 1
    else
        info "Skipped launchd install. Plist is available at:"
        info "  ${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist"
    fi
else
    warn "No supported service manager found (systemd or launchd)."
    info "Use the portable runner:"
    info "  ${DAEMON_DIR}/run.sh"
    INSTALL_DAEMON="n"
fi

# ---------------------------------------------------------------------------
# Final validation tests
# ---------------------------------------------------------------------------
run_tests() {
    local base_url="http://127.0.0.1:${PORT}"
    local curl_auth=()
    if [[ -n "$API_KEY" ]]; then
        curl_auth=(-H "Authorization: Bearer ${API_KEY}")
    fi

    echo ""
    echo -e "${BOLD}Validation tests${RESET}"
    echo "────────────────────────────────────────────────────────────────"

    info "Waiting for the bridge to start ..."
    for _ in {1..12}; do
        if curl -s -o /dev/null -w "%{http_code}" "$base_url/health" 2>/dev/null | grep -q '^200$'; then
            break
        fi
        sleep 1
    done

    info "Testing /health ..."
    local health_status
    health_status=$(curl -s -o /dev/null -w "%{http_code}" "${curl_auth[@]}" "$base_url/health" 2>/dev/null || true)
    if [[ "$health_status" == "200" ]]; then
        success "/health returned 200."
        curl -s "${curl_auth[@]}" "$base_url/health" | sed 's/^/     /'
    else
        error "/health returned ${health_status:-no response}."
        return 1
    fi

    info "Testing /v1/models ..."
    local models_status
    models_status=$(curl -s -o /dev/null -w "%{http_code}" "${curl_auth[@]}" "$base_url/v1/models" 2>/dev/null || true)
    if [[ "$models_status" == "200" ]]; then
        success "/v1/models returned 200."
    else
        error "/v1/models returned ${models_status:-no response}."
        return 1
    fi

    echo ""
    success "All validation tests passed!"
    return 0
}

if [[ "${INSTALL_DAEMON:-Y}" =~ ^[Yy]$ ]]; then
    run_tests
else
    info "Skipping validation tests because the daemon was not started."
    info "After starting the bridge, test it with:"
    info "  curl -s http://127.0.0.1:${PORT}/health | jq"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo "────────────────────────────────────────────────────────────────"
info "Working directory: ${REPO_DIR}"
info "Listen port:       ${PORT}"
if [[ -n "$API_KEY" ]]; then
    info "API key:           ${API_KEY}"
else
    info "API key:           (none / no client auth)"
fi

info "Check the logs:"
echo "     tail -f ${REPO_DIR}/bridge.log"

if [[ "$DETECTED_INIT" == "systemd" ]]; then
    info "Manage the service:"
    echo "     sudo systemctl status anthropic-oauth-bridge"
    echo "     sudo systemctl restart anthropic-oauth-bridge"
    echo "     sudo systemctl stop anthropic-oauth-bridge"
elif [[ "$DETECTED_INIT" == "launchd" ]]; then
    info "Manage the agent:"
    echo "     launchctl list com.lomelidev.anthropic-oauth-bridge"
    echo "     launchctl stop com.lomelidev.anthropic-oauth-bridge"
    echo "     launchctl start com.lomelidev.anthropic-oauth-bridge"
else
    info "Start manually:"
    echo "     ${DAEMON_DIR}/run.sh"
fi

info "Configure Hermes:"
echo "     ./scripts/add-to-hermes.sh"
info "Configure OpenClaw:"
echo "     ./scripts/add-to-openclaw.sh"
info "Test the bridge:"
echo "     curl -s http://127.0.0.1:${PORT}/health | jq"

echo ""
echo -e "${CYAN}Happy bridging!${RESET}"
