#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CODEX_SESSIONS_REPO_URL:-https://github.com/stephenjoly/codex-session-indexer.git}"
INSTALL_DIR="${CODEX_SESSIONS_INSTALL_DIR:-$HOME/.codex/codex-session-indexer}"
GLOBAL_ROOT="${CODEX_SESSIONS_GLOBAL_ROOT:-$HOME/Documents/Coding}"
BIN_DIR="${CODEX_SESSIONS_BIN_DIR:-$HOME/.local/bin}"
INSTALL_DAEMON=0
SKIP_INITIAL_SYNC=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --repo-url URL         Git repository URL or local path to clone/pull from
  --install-dir PATH     Directory where the project will be installed
  --global-root PATH     Root directory for the generated global index
  --bin-dir PATH         Directory where the codex-sessions symlink is created
  --daemon               Install and start a macOS launchd watcher
  --no-daemon            Skip daemon installation
  --skip-initial-sync    Do not run the initial codex-sessions generate
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)
      REPO_URL="$2"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --global-root)
      GLOBAL_ROOT="$2"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="$2"
      shift 2
      ;;
    --daemon)
      INSTALL_DAEMON=1
      shift
      ;;
    --no-daemon)
      INSTALL_DAEMON=0
      shift
      ;;
    --skip-initial-sync)
      SKIP_INITIAL_SYNC=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd git
need_cmd python3
need_cmd tar

mkdir -p "$HOME/.codex"
mkdir -p "$BIN_DIR"

if [[ "$REPO_URL" == *"YOUR_GITHUB_USER"* ]]; then
  echo "Set CODEX_SESSIONS_REPO_URL or pass --repo-url before using the published curl command." >&2
  exit 1
fi

copy_local_checkout() {
  local source_dir="$1"
  echo "Copying local checkout into $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  tar \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    -cf - \
    -C "$source_dir" . | tar -xf - -C "$INSTALL_DIR"
}

if [[ -d "$REPO_URL" ]]; then
  copy_local_checkout "$REPO_URL"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Updating existing install at $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --tags origin
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "Cloning into $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "Creating virtual environment"
python3 -m venv "$INSTALL_DIR/.venv"

echo "Installing codex-session-indexer"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"

ln -sf "$INSTALL_DIR/.venv/bin/codex-sessions" "$BIN_DIR/codex-sessions"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  cat <<EOF

Add this to your shell profile so 'codex-sessions' is on PATH:
  export PATH="$BIN_DIR:\$PATH"
EOF
fi

if [[ "$SKIP_INITIAL_SYNC" -eq 0 ]]; then
  echo "Running initial sync"
  "$INSTALL_DIR/.venv/bin/codex-sessions" generate --global-root "$GLOBAL_ROOT"
fi

install_launchd_daemon() {
  local label="com.codex-session-indexer.watch"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
      <string>$INSTALL_DIR/.venv/bin/codex-sessions</string>
      <string>watch</string>
      <string>--global-root</string>
      <string>$GLOBAL_ROOT</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$HOME/.codex/codex-session-indexer.out.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME/.codex/codex-session-indexer.err.log</string>
  </dict>
</plist>
EOF

  launchctl unload "$plist" >/dev/null 2>&1 || true
  launchctl load "$plist"
  launchctl start "$label" >/dev/null 2>&1 || true

  echo "Installed macOS watcher via launchd:"
  echo "  $plist"
}

if [[ "$INSTALL_DAEMON" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Daemon auto-install is only implemented for macOS launchd right now." >&2
    exit 1
  fi
  need_cmd launchctl
  install_launchd_daemon
fi

cat <<EOF

Setup complete.

Install directory:
  $INSTALL_DIR

Global root:
  $GLOBAL_ROOT

Binary:
  $BIN_DIR/codex-sessions

Manual run:
  codex-sessions generate --global-root "$GLOBAL_ROOT" --verbose

Watcher:
  codex-sessions watch --global-root "$GLOBAL_ROOT" --verbose
EOF
