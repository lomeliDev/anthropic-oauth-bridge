#!/usr/bin/env bash
#
# Anthropic OAuth → OpenAI Bridge installer
# Supports: Linux (systemd), macOS (launchd), and a portable fallback script.
#
# Usage:
#   ./install.sh              # Quick install (Python + deps only, use built-in PKCE)
#   ./install.sh --full       # Full install (adds OpenCode + Claude Code CLI)
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
MODE="quick"
for arg in "$@"; do
    case "$arg" in
        --full|-f) MODE="full" ;;
        --help|-h)
            echo "Usage: ./install.sh [--quick|--full]"
            echo "  --quick   Install Python deps + start bridge (use built-in PKCE OAuth)"
            echo "  --full    Also install OpenCode + Claude Code CLI (legacy)"
            exit 0
            ;;
    esac
done

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
    local label="Anthropic OAuth Bridge installer"
    [[ "$MODE" == "quick" ]] && label="$label (quick)"
    [[ "$MODE" == "full" ]] && label="$label (full)"
    echo ""
    echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════════${RESET}"
    echo -e "${CYAN}${BOLD}  $label${RESET}"
    echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════════${RESET}"
    echo ""
}

info()    { echo -e "${CYAN}ℹ${RESET}  $*"; }
success() { echo -e "${GREEN}✔${RESET}  $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✖${RESET}  $*"; }

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_PORT=64173
DEFAULT_CLAUDE_CREDENTIALS="${CLAUDE_CREDENTIALS_PATH:-$HOME/.claude/.credentials.json}"
DEFAULT_ANTHROPIC_CLIENT_ID="${ANTHROPIC_CLIENT_ID:-9d1c250a-e61b-44d9-88ed-5944d1962f5e}"

CURL="curl -fsSL --connect-timeout 10 --max-time 120"

# ---------------------------------------------------------------------------
# Quick mode: just Python + deps + config
# ---------------------------------------------------------------------------
quick_install() {
    print_header

    if [[ "$EUID" -eq 0 ]]; then
        warn "Running as root. Service will be configured for root."
        read -rp "Continue? [y/N]: " c
        [[ "${c:-N}" =~ ^[Yy]$ ]] || exit 0
    fi

    echo ""
    echo -e "${BOLD}What this does${RESET}"
    echo "────────────────────────────────────────────────────────────────"
    echo "  1. Check Python 3.9+"
    echo "  2. Create virtual environment and install deps"
    echo "  3. Ask for port, optional API key"
    echo "  4. Install daemon (systemd/launchd) and start"
    echo ""
    echo -e "${CYAN}After install, run: python3 auth-login.py${RESET}"
    echo -e "${CYAN}to authenticate via the built-in PKCE OAuth flow.${RESET}"
    echo ""

    # --- Python check ---
    if ! command -v python3 >/dev/null 2>&1; then
        error "python3 not found. Install Python 3.9+ first."
        exit 1
    fi
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 9 ]]; }; then
        error "Python 3.9+ required. Found ${PY_MAJOR}.${PY_MINOR}."
        exit 1
    fi
    success "Python ${PY_MAJOR}.${PY_MINOR} ready."

    # --- Venv + deps ---
    if [[ ! -d ".venv" ]]; then
        info "Creating virtual environment..."
        python3 -m venv .venv
    fi
    info "Installing dependencies..."
    .venv/bin/pip install -q -r requirements.txt || {
        error "Failed to install Python dependencies."
        exit 1
    }
    success "Dependencies installed."

    # Detect platform
    DETECTED_OS="unknown"
    DETECTED_INIT="none"
    if [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$OSTYPE" == "linux"* ]]; then
        DETECTED_OS="linux"
        command -v systemctl >/dev/null 2>&1 && DETECTED_INIT="systemd"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        DETECTED_OS="macos"
        command -v launchctl >/dev/null 2>&1 && DETECTED_INIT="launchd"
    fi
    info "Platform: ${DETECTED_OS} (${DETECTED_INIT})"

    # --- Config ---
    echo ""
    echo -e "${BOLD}Configuration${RESET}"
    echo "────────────────────────────────────────────────────────────────"
    read -rp "Listen port [${DEFAULT_PORT}]: " PORT
    PORT="${PORT:-$DEFAULT_PORT}"
    [[ "$PORT" =~ ^[0-9]+$ ]] && [[ "$PORT" -ge 1 ]] && [[ "$PORT" -le 65535 ]] || {
        error "Invalid port: ${PORT}"
        exit 1
    }

    RANDOM_KEY="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"
    read -rp "Require an API key for clients? [Y/n]: " NEED_KEY
    NEED_KEY="${NEED_KEY:-Y}"
    API_KEY=""
    if [[ "$NEED_KEY" =~ ^[Yy]$ ]]; then
        read -rp "API key [random]: " API_KEY
        API_KEY="${API_KEY:-$RANDOM_KEY}"
        success "API key set."
    else
        info "No client auth (open bridge)."
    fi

    # --- Write .env ---
    cat > .env <<EOF
PORT=${PORT}
CLAUDE_CREDENTIALS_PATH=${DEFAULT_CLAUDE_CREDENTIALS}
ANTHROPIC_CLIENT_ID=${DEFAULT_ANTHROPIC_CLIENT_ID}
ANTHROPIC_CLI_VERSION=2.1.202
EOF
    [[ -n "$API_KEY" ]] && echo "BRIDGE_API_KEY=${API_KEY}" >> .env
    chmod 600 .env
    success "Wrote .env"

    # --- Daemon ---
    DAEMON_DIR="${REPO_DIR}/daemon"
    mkdir -p "$DAEMON_DIR"
    PYTHON_BIN_DIR="${REPO_DIR}/.venv/bin"

    # systemd
    if [[ "$DETECTED_INIT" == "systemd" ]]; then
        cat > "${DAEMON_DIR}/anthropic-oauth-bridge.service" <<EOF
[Unit]
Description=Anthropic OAuth Bridge
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${REPO_DIR}/.env
ExecStart=${PYTHON_BIN_DIR}/python3 ${REPO_DIR}/server.py --port ${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    fi

    # launchd
    if [[ "$DETECTED_INIT" == "launchd" ]]; then
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
        <string>--port</string>
        <string>${PORT}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORT</key><string>${PORT}</string>
        <key>CLAUDE_CREDENTIALS_PATH</key><string>${DEFAULT_CLAUDE_CREDENTIALS}</string>
        <key>ANTHROPIC_CLIENT_ID</key><string>${DEFAULT_ANTHROPIC_CLIENT_ID}</string>
EOF
        [[ -n "$API_KEY" ]] && echo "        <key>BRIDGE_API_KEY</key><string>${API_KEY}</string>" >> "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist"
        cat >> "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" <<EOF
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${REPO_DIR}/bridge.log</string>
    <key>StandardErrorPath</key><string>${REPO_DIR}/bridge.log</string>
</dict>
</plist>
EOF
    fi

    # Portable runner
    cat > "${DAEMON_DIR}/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${REPO_DIR}"
source .env
exec ${PYTHON_BIN_DIR}/python3 server.py --port ${PORT}
EOF
    chmod +x "${DAEMON_DIR}/run.sh"
    success "Daemon files written."

    # --- Install daemon ---
    echo ""
    echo -e "${BOLD}Start service${RESET}"
    echo "────────────────────────────────────────────────────────────────"

    if [[ "$DETECTED_INIT" == "systemd" ]]; then
        read -rp "Install and start systemd service? [Y/n]: " INSTALL
        if [[ "${INSTALL:-Y}" =~ ^[Yy]$ ]]; then
            local unit="${DAEMON_DIR}/anthropic-oauth-bridge.service"
            sudo cp "$unit" /etc/systemd/system/ || cp "$unit" /etc/systemd/system/
            sudo systemctl daemon-reload
            sudo systemctl enable --now anthropic-oauth-bridge
            success "systemd service started."
        fi
    elif [[ "$DETECTED_INIT" == "launchd" ]]; then
        read -rp "Install and start launchd agent? [Y/n]: " INSTALL
        if [[ "${INSTALL:-Y}" =~ ^[Yy]$ ]]; then
            mkdir -p "$HOME/Library/LaunchAgents"
            cp "${DAEMON_DIR}/com.lomelidev.anthropic-oauth-bridge.plist" "$HOME/Library/LaunchAgents/"
            launchctl unload "$HOME/Library/LaunchAgents/com.lomelidev.anthropic-oauth-bridge.plist" 2>/dev/null || true
            launchctl load -w "$HOME/Library/LaunchAgents/com.lomelidev.anthropic-oauth-bridge.plist"
            success "launchd agent started."
        fi
    else
        warn "No systemd/launchd detected. Run manually:"
        info "  ${DAEMON_DIR}/run.sh"
    fi

    # --- Quick validation ---
    sleep 2
    local ok=0
    curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q "200" && ok=1
    if [[ "$ok" -eq 1 ]]; then
        success "Bridge is running on http://127.0.0.1:${PORT}"
    else
        warn "Bridge may not be running yet. Check: ${REPO_DIR}/bridge.log"
    fi

    echo ""
    echo -e "${GREEN}${BOLD}Done!${RESET}"
    echo "────────────────────────────────────────────────────────────────"
    echo "  Bridge:  http://127.0.0.1:${PORT}"
    [[ -n "$API_KEY" ]] && echo "  API key: ${API_KEY}"
    echo ""
    echo "  📌 Next step — authenticate:"
    echo "     python3 auth-login.py"
    echo ""
    echo "  🔧 Manage:"
    echo "     tail -f bridge.log"
    [[ "$DETECTED_INIT" == "systemd" ]] && echo "     sudo systemctl status anthropic-oauth-bridge"
    echo ""
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [[ "$MODE" == "quick" ]]; then
    quick_install
    exit 0
fi

# Full mode: OpenCode + Claude Code (legacy path, kept for reference)
echo "Full mode not yet implemented — use --quick for now."
echo "Quick mode is the recommended path."
exit 1
