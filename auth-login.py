#!/usr/bin/env python3
"""
PKCE OAuth login for Anthropic Claude — standalone script.
No Claude Code CLI, no OpenCode needed.

Usage:
  python3 auth-login.py                # Step 1: Generate URL
  python3 auth-login.py <code>         # Step 2: Exchange code for tokens
  python3 auth-login.py <code> <label> # Step 2 + register as multi-account

Tokens are saved to ~/.claude/.credentials.json (bridge-compatible format)
and optionally registered in accounts.json for multi-account.
"""
import hashlib, base64, os, json, sys, urllib.parse, secrets, time
import requests

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
AUTH_ENDPOINT = "https://claude.ai/oauth/authorize"
# Use claude.ai (not platform.claude.com) — claude.ai doesn't rate-limit datacenter IPs
TOKEN_ENDPOINT = "https://claude.ai/v1/oauth/token"
SCOPE = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
STATE_FILE = os.path.expanduser("~/.cache/anthropic-pkce-state.json")
CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")


def generate_pkce():
    """Generate code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def get_authorize_url(code_challenge, state):
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code, code_verifier):
    # Strip fragment if present (#state)
    code = code.split("#")[0].strip()

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "claude-code/2.1.202 (external, cli)",
    }
    resp = requests.post(TOKEN_ENDPOINT, data=data, headers=headers, timeout=30)
    print(f"HTTP {resp.status_code}")

    if resp.status_code == 429:
        print("Rate limited. Try again later or run the curl on a residential IP.")
        print(f"  curl -s {TOKEN_ENDPOINT} -H 'Content-Type: application/x-www-form-urlencoded' \\")
        print(f"    -d 'grant_type=authorization_code&code={code}&client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&code_verifier={code_verifier}'")
        return None

    resp.raise_for_status()
    tokens = resp.json()

    # Save to .credentials.json (bridge-compatible format)
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    cred = {
        "claudeAiOauth": {
            "accessToken": tokens["access_token"],
            "refreshToken": tokens["refresh_token"],
            "expiresAt": int(time.time() * 1000) + (tokens["expires_in"] * 1000),
            "scopes": tokens["scope"].split(),
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        }
    }
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(cred, f, indent=2)
    print(f"\n✅ Tokens saved to {CREDENTIALS_FILE}")
    print(f"   Email: {tokens.get('account', {}).get('email_address', '?')}")
    print(f"   Expires in: {tokens['expires_in']}s ({tokens['expires_in']/3600:.1f}h)")

    return tokens


def register_account(api_key, label, refresh_token):
    """Register in accounts.json for multi-account."""
    accounts_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
    if os.path.exists(accounts_file):
        with open(accounts_file) as f:
            accounts = json.load(f)
    else:
        accounts = {"accounts": {}}

    accounts["accounts"][api_key] = {
        "label": label,
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    with open(accounts_file, "w") as f:
        json.dump(accounts, f, indent=2)
    print(f"\n📋 Account '{label}' registered in {accounts_file}")


def step1_generate():
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)

    # Persist state
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({
            "code_verifier": code_verifier,
            "state": state,
            "code_challenge": code_challenge,
        }, f, indent=2)

    url = get_authorize_url(code_challenge, state)

    print("=" * 70)
    print("🔑  Abre este link en tu navegador:")
    print("=" * 70)
    print(url)
    print("=" * 70)
    print("\nLuego ejecuta:")
    print("  python3 auth-login.py <CODIGO_DE_LA_URL>")
    print(f"  (o python3 auth-login.py <CODIGO> <label> para multi-account)")
    print(f"\n(Estado guardado en {STATE_FILE})")


def step2_exchange(code, label=None):
    if not os.path.exists(STATE_FILE):
        print("❌ No hay sesión PKCE activa. Ejecuta sin argumentos primero.")
        sys.exit(1)

    with open(STATE_FILE) as f:
        state_data = json.load(f)

    print(f"🔁 Intercambiando código: {code[:20]}...")
    print(f"   Verifier: {state_data['code_verifier'][:20]}...")
    print()

    tokens = exchange_code(code, state_data["code_verifier"])

    if tokens:
        if label:
            register_account(f"sk-{label.lower().replace(' ', '-')}", label, tokens["refresh_token"])

        # Cleanup state
        os.remove(STATE_FILE)
        print("🧹 Estado PKCE limpiado.")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        step1_generate()
    elif len(sys.argv) == 2:
        step2_exchange(sys.argv[1])
    else:
        step2_exchange(sys.argv[1], label=sys.argv[2])
