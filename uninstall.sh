#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${CODEX_SESSIONS_INSTALL_DIR:-$HOME/.codex/codex-session-indexer}"
BIN_DIR="${CODEX_SESSIONS_BIN_DIR:-$HOME/.local/bin}"
PIPX_HOME="${CODEX_SESSIONS_PIPX_HOME:-$HOME/.local/pipx}"
KEEP_STATE=0

usage() {
  cat <<'EOF'
Usage: uninstall.sh [options]

Options:
  --install-dir PATH   Directory where codex-session-indexer is installed
  --bin-dir PATH       Directory containing the codex-sessions symlink
  --keep-state         Keep ~/.codex state and log files
  -h, --help           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="$2"
      shift 2
      ;;
    --keep-state)
      KEEP_STATE=1
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

remove_launchd_daemon() {
  local label="com.codex-session-indexer.watch"
  local plist="$HOME/Library/LaunchAgents/$label.plist"

  if [[ -f "$plist" ]]; then
    if command -v launchctl >/dev/null 2>&1; then
      launchctl unload "$plist" >/dev/null 2>&1 || true
      launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    fi
    rm -f "$plist"
    echo "Removed launchd watcher: $plist"
  fi
}

remove_pipx_install() {
  if ! command -v pipx >/dev/null 2>&1; then
    return
  fi

  if PIPX_HOME="$PIPX_HOME" PIPX_BIN_DIR="$BIN_DIR" pipx uninstall codex-session-indexer >/dev/null 2>&1; then
    echo "Removed pipx install: codex-session-indexer"
  fi
}

remove_symlink() {
  local link_path="$BIN_DIR/codex-sessions"
  if [[ -L "$link_path" || -f "$link_path" ]]; then
    rm -f "$link_path"
    echo "Removed binary link: $link_path"
  fi
}

remove_install_dir() {
  if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "Removed install directory: $INSTALL_DIR"
  fi
}

remove_state_files() {
  local state_file="$HOME/.codex/codex-session-indexer-state.json"
  local out_log="$HOME/.codex/codex-session-indexer.out.log"
  local err_log="$HOME/.codex/codex-session-indexer.err.log"

  rm -f "$state_file" "$out_log" "$err_log"
  echo "Removed state and log files from ~/.codex"
}

remove_launchd_daemon
remove_pipx_install
remove_symlink
remove_install_dir

if [[ "$KEEP_STATE" -eq 0 ]]; then
  remove_state_files
fi

cat <<EOF

Uninstall complete.

Not removed:
  - generated codex-sessions.md files in your project directories
  - generated codex-sessions-index.md files under your chosen global root

Those outputs are left in place on purpose to avoid deleting project files unexpectedly.
EOF
