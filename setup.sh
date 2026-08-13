#!/usr/bin/env bash
# One-click setup for the Spanish Immigration Status Notifier.
#
#   ./setup.sh              interactive install (venv, browser, .env, optional systemd)
#   ./setup.sh --no-prompt  non-interactive: installs everything, skips questions
#
# Safe to re-run: every step is idempotent.

set -euo pipefail
cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
say()  { echo "${BOLD}==>${RESET} $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

PROMPT=1
[ "${1:-}" = "--no-prompt" ] && PROMPT=0

# ── 1. Python ─────────────────────────────────────────────────────
say "Checking Python…"
PY=$(command -v python3 || true)
[ -n "$PY" ] || fail "python3 not found. Install Python 3.11+ and re-run."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 3.11+ required (found $("$PY" -V))."
echo "    $("$PY" -V) ✓"

# ── 2. Tesseract (free OCR captcha solver) ────────────────────────
say "Checking Tesseract OCR…"
if ! command -v tesseract >/dev/null; then
    if command -v apt-get >/dev/null; then
        say "Installing tesseract-ocr (needs sudo)…"
        sudo apt-get update -qq && sudo apt-get install -y -qq tesseract-ocr
    elif command -v dnf >/dev/null; then
        sudo dnf install -y tesseract
    elif command -v brew >/dev/null; then
        brew install tesseract
    else
        fail "tesseract not found and no known package manager. Install it manually, then re-run."
    fi
fi
echo "    $(tesseract --version 2>&1 | head -1) ✓"

# ── 3. Virtualenv + Python dependencies ───────────────────────────
say "Creating virtualenv and installing dependencies…"
[ -d venv ] || "$PY" -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "    dependencies installed ✓"

# ── 4. Playwright Chromium ────────────────────────────────────────
say "Installing the Chromium browser for Playwright (first run downloads ~120 MB)…"
if ! venv/bin/playwright install --with-deps chromium 2>/dev/null; then
    echo "    --with-deps needs root; retrying without system deps…"
    venv/bin/playwright install chromium
    echo "    NOTE: if the app later fails to launch Chromium, run:"
    echo "          sudo venv/bin/playwright install-deps chromium"
fi
echo "    Chromium ready ✓"

# ── 5. Configuration ──────────────────────────────────────────────
if [ ! -f .env ]; then
    say "Creating .env from .env.example…"
    cp .env.example .env
    if [ "$PROMPT" = 1 ]; then
        echo
        echo "Telegram alerts are optional but recommended."
        echo "Create a bot with @BotFather in Telegram and paste its token here,"
        echo "or press Enter to skip (you can edit .env later)."
        read -r -p "TELEGRAM_TOKEN: " token
        if [ -n "$token" ]; then
            sed -i.bak "s|^TELEGRAM_TOKEN=.*|TELEGRAM_TOKEN=$token|" .env && rm -f .env.bak
        fi
    fi
else
    say ".env already exists — keeping it."
fi

# ── 6. Sanity check ───────────────────────────────────────────────
say "Running unit tests…"
if venv/bin/python -m pytest -q >/dev/null 2>&1; then
    echo "    tests pass ✓"
else
    echo "    (pytest not installed or tests failed — not fatal, continuing)"
fi

# ── 7. Optional: systemd service ──────────────────────────────────
install_service() {
    local unit=/etc/systemd/system/immigration-notifier.service
    say "Installing systemd service…"
    sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=Spanish Immigration Status Notifier
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python -m app.web.main
Restart=on-failure
RestartSec=10
User=$(whoami)

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now immigration-notifier
    echo "    service running ✓  (logs: journalctl -u immigration-notifier -f)"
}

if command -v systemctl >/dev/null && [ "$PROMPT" = 1 ]; then
    echo
    read -r -p "Install as a systemd service so it starts on boot? [y/N] " yn
    case "$yn" in [Yy]*) install_service ;; esac
fi

# ── Done ──────────────────────────────────────────────────────────
echo
say "Setup complete!"
if systemctl is-active immigration-notifier >/dev/null 2>&1; then
    echo "The app is running as a service → open http://127.0.0.1:8000"
else
    echo "Start the app with:   venv/bin/python -m app.web.main"
    echo "Then open:            http://127.0.0.1:8000"
fi
echo "Edit .env to configure Telegram, captcha provider, and check intervals."
