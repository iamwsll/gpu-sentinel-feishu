#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="gpu-sentinel"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

say() {
  printf '%s\n' "$*"
}

warn() {
  printf '%s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-}"
  local answer
  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n] " answer
      answer="${answer:-y}"
    elif [[ "$default" == "n" ]]; then
      read -r -p "$prompt [y/N] " answer
      answer="${answer:-n}"
    else
      read -r -p "$prompt [y/n] " answer
    fi
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) warn "Please answer y or n." ;;
    esac
  done
}

expand_path() {
  python3 - "$1" <<'PY'
import os
import sys

raw = sys.argv[1].strip()
if not raw:
    raise SystemExit("empty path")
expanded = os.path.expanduser(raw)
if not os.path.isabs(expanded):
    raise SystemExit("path must be absolute or start with ~")
print(os.path.abspath(expanded))
PY
}

ask_install_dir() {
  local raw
  local expanded
  if ask_yes_no "Use this install path: $SCRIPT_DIR ?" "y"; then
    printf '%s\n' "$SCRIPT_DIR"
    return 0
  fi
  while true; do
    read -r -p "Install path to clean up: " raw
    if expanded="$(expand_path "$raw" 2>/dev/null)"; then
      printf '%s\n' "$expanded"
      return 0
    fi
    warn "Please enter an absolute path or a ~ path."
  done
}

safe_remove_dir() {
  local target="$1"
  [[ -n "$target" ]] || die "Refusing to delete an empty path"
  [[ "$target" != "/" ]] || die "Refusing to delete /"
  [[ "$target" != "$HOME" ]] || die "Refusing to delete HOME"
  [[ "$target" == "$HOME"* || "$target" == /tmp/* || "$target" == /var/tmp/* || "$target" == /opt/* || "$target" == /usr/local/* ]] || die "Refusing to delete unexpected path: $target"
  rm -rf -- "$target"
}

main() {
  say "GPU Sentinel uninstaller"
  local install_dir
  install_dir="$(ask_install_dir)"

  systemctl --user disable --now "$SERVICE_NAME.timer" >/dev/null 2>&1 || true
  systemctl --user stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
  rm -f -- "$HOME/.config/systemd/user/$SERVICE_NAME.service" "$HOME/.config/systemd/user/$SERVICE_NAME.timer"
  systemctl --user daemon-reload >/dev/null 2>&1 || true

  if ask_yes_no "Delete install directory and all data inside it, including config.json and gpu_sentinel.sqlite3?" "n"; then
    safe_remove_dir "$install_dir"
    say "Deleted: $install_dir"
  else
    say "Kept install directory: $install_dir"
  fi

  say "Uninstall complete."
}

main "$@"
