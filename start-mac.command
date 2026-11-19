#!/bin/bash
# ClipViral one-click launcher for macOS.
# Double-click this file in Finder (first time: right-click > Open).
# It installs what's missing (Homebrew packages, Python 3.12, ffmpeg),
# sets up the app, starts it and opens your browser.

cd "$(dirname "$0")" || exit 1
set -o pipefail

say() { printf "\n\033[1;33m==> %s\033[0m\n" "$1"; }
fail() {
  printf "\n\033[1;31mERROR: %s\033[0m\n" "$1"
  echo "Copy everything in this window and send it for help."
  read -r -p "Press Enter to close..." _
  exit 1
}

# 1. Homebrew
if ! command -v brew >/dev/null 2>&1; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)"
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (it will ask for your Mac password)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "Homebrew install failed."
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)"
  done
fi

# 2. Python 3.12 and ffmpeg
for pkg in python@3.12 ffmpeg; do
  if ! brew list --versions "$pkg" >/dev/null 2>&1; then
    say "Installing $pkg (one time, can take a few minutes)"
    brew install "$pkg" || fail "Could not install $pkg."
  fi
done
if ! brew list --cask font-inter >/dev/null 2>&1; then
  say "Installing the Inter font for captions"
  brew install --cask font-inter >/dev/null 2>&1 || echo "(font install skipped; captions will use a fallback font)"
fi
PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -x "$PY" ] || fail "Python 3.12 not found at $PY"

# 3. App environment
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  say "Creating the app environment"
  rm -rf .venv
  "$PY" -m venv .venv || fail "Could not create the Python environment."
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  say "Installing app packages (one time, a few minutes)"
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt || fail "Package install failed."
  touch .venv/.installed
fi

# 4. API key (optional)
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Optional: paste your Anthropic API key for AI-picked clips (starts with sk-ant-)."
  echo "Leave it empty and press Enter to use the built-in scorer instead."
  read -r -p "API key: " KEY
  if [ -n "$KEY" ]; then
    sed -i '' "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$KEY|" .env
  fi
fi

# 5. Start
PORT=8000
while lsof -i ":$PORT" >/dev/null 2>&1; do PORT=$((PORT + 1)); done
say "Starting ClipViral at http://localhost:$PORT/studio  (close this window to stop it)"
( sleep 3; open "http://localhost:$PORT/studio" ) &
exec .venv/bin/python -m uvicorn clipviral.main:app --host 127.0.0.1 --port "$PORT"
