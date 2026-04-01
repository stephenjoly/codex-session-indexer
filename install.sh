#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CODEX_SESSIONS_REPO_URL:-https://github.com/stephenjoly/codex-session-indexer.git}"
INSTALL_DIR="${CODEX_SESSIONS_INSTALL_DIR:-$HOME/.codex/codex-session-indexer}"
GLOBAL_ROOT="${CODEX_SESSIONS_GLOBAL_ROOT:-$HOME/Documents/Coding}"
BIN_DIR="${CODEX_SESSIONS_BIN_DIR:-$HOME/.local/bin}"
PIPX_HOME="${CODEX_SESSIONS_PIPX_HOME:-$HOME/.local/pipx}"
INSTALL_DAEMON=0
SKIP_INITIAL_SYNC=0
INSTALL_METHOD="auto"
SELECTED_INSTALL_METHOD=""
CLI_PATH=""

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --repo-url URL         Git repository URL or local path to install from
  --install-dir PATH     Directory for fallback venv installs
  --global-root PATH     Root directory for the generated global index
  --bin-dir PATH         Directory where the codex-sessions binary is placed
  --pipx                 Force pipx installation
  --venv                 Force fallback venv installation
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
    --pipx)
      INSTALL_METHOD="pipx"
      shift
      ;;
    --venv)
      INSTALL_METHOD="venv"
      shift
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

is_local_source() {
  [[ -d "$REPO_URL" ]]
}

github_archive_url() {
  local repo_url="$1"
  local normalized="${repo_url%/}"
  normalized="${normalized%.git}"
  if [[ "$normalized" =~ ^https://github\.com/([^/]+)/([^/]+)$ ]]; then
    printf 'https://github.com/%s/%s/archive/refs/heads/main.tar.gz\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

copy_local_checkout() {
  local source_dir="$1"
  echo "Copying local checkout into $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  tar \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.git' \
    -cf - \
    -C "$source_dir" . | tar -xf - -C "$INSTALL_DIR"
}

download_archive_checkout() {
  local archive_url="$1"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  echo "Downloading source archive"
  curl -fsSL "$archive_url" -o "$tmpdir/source.tar.gz"
  tar -xzf "$tmpdir/source.tar.gz" -C "$tmpdir"

  local extracted_dir
  extracted_dir="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$extracted_dir" ]]; then
    echo "Failed to unpack source archive." >&2
    exit 1
  fi

  rm -rf "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  tar \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    -cf - \
    -C "$extracted_dir" . | tar -xf - -C "$INSTALL_DIR"
}

prepare_fallback_source() {
  if is_local_source; then
    copy_local_checkout "$REPO_URL"
    return
  fi

  local archive_url=""
  if archive_url="$(github_archive_url "$REPO_URL")"; then
    download_archive_checkout "$archive_url"
    return
  fi

  need_cmd git
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "Updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --tags origin
    git -C "$INSTALL_DIR" pull --ff-only
  else
    echo "Cloning into $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
}

install_with_venv() {
  need_cmd python3
  need_cmd tar
  if ! is_local_source; then
    need_cmd curl
  fi

  prepare_fallback_source

  echo "Creating virtual environment"
  python3 -m venv "$INSTALL_DIR/.venv"

  echo "Installing codex-session-indexer"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$INSTALL_DIR/.venv/bin/python" -m pip install "$INSTALL_DIR"

  mkdir -p "$BIN_DIR"
  ln -sf "$INSTALL_DIR/.venv/bin/codex-sessions" "$BIN_DIR/codex-sessions"
  CLI_PATH="$BIN_DIR/codex-sessions"
  SELECTED_INSTALL_METHOD="venv"
}

install_with_pipx() {
  need_cmd pipx

  local source_spec=""
  if is_local_source; then
    source_spec="$REPO_URL"
  elif source_spec="$(github_archive_url "$REPO_URL")"; then
    :
  else
    need_cmd git
    source_spec="git+$REPO_URL"
  fi

  mkdir -p "$BIN_DIR" "$PIPX_HOME"

  echo "Installing codex-session-indexer with pipx"
  PIPX_HOME="$PIPX_HOME" PIPX_BIN_DIR="$BIN_DIR" pipx install --force "$source_spec"
  CLI_PATH="$BIN_DIR/codex-sessions"
  SELECTED_INSTALL_METHOD="pipx"
}

ensure_bin_dir_visible() {
  if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    cat <<EOF

Add this to your shell profile so 'codex-sessions' is on PATH:
  export PATH="$BIN_DIR:\$PATH"
EOF
  fi
}

run_initial_sync() {
  if [[ "$SKIP_INITIAL_SYNC" -eq 0 ]]; then
    echo "Running initial sync"
    "$CLI_PATH" generate --global-root "$GLOBAL_ROOT"
  fi
}

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
      <string>$CLI_PATH</string>
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

mkdir -p "$HOME/.codex"

if [[ "$REPO_URL" == *"YOUR_GITHUB_USER"* ]]; then
  echo "Set CODEX_SESSIONS_REPO_URL or pass --repo-url before using the published curl command." >&2
  exit 1
fi

case "$INSTALL_METHOD" in
  auto)
    if command -v pipx >/dev/null 2>&1; then
      install_with_pipx
    else
      install_with_venv
    fi
    ;;
  pipx)
    install_with_pipx
    ;;
  venv)
    install_with_venv
    ;;
  *)
    echo "Unsupported install method: $INSTALL_METHOD" >&2
    exit 1
    ;;
esac

ensure_bin_dir_visible
run_initial_sync

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

Install method:
  $SELECTED_INSTALL_METHOD

Install directory:
  $INSTALL_DIR

Global root:
  $GLOBAL_ROOT

Binary:
  $CLI_PATH

Manual run:
  codex-sessions generate --global-root "$GLOBAL_ROOT" --verbose

Watcher:
  codex-sessions watch --global-root "$GLOBAL_ROOT" --verbose
EOF
