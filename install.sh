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

need_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
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
  while true; do
    read -r -p "Install path (required, absolute path or ~ path): " raw
    if [[ -z "${raw// }" ]]; then
      warn "Install path is required; no default is provided."
      continue
    fi
    if ! expanded="$(expand_path "$raw" 2>/dev/null)"; then
      warn "Please enter an absolute path, such as /opt/gpu-sentinel, or a ~ path."
      continue
    fi
    if [[ "$expanded" =~ [[:space:]] ]]; then
      warn "Install path must not contain whitespace because systemd ExecStart paths are whitespace-sensitive."
      continue
    fi
    if [[ -e "$expanded" ]]; then
      if ask_yes_no "Path exists: $expanded. Continue and overwrite managed files inside it?" "n"; then
        printf '%s\n' "$expanded"
        return 0
      fi
      continue
    fi
    printf '%s\n' "$expanded"
    return 0
  done
}

ask_required() {
  local prompt="$1"
  local value
  while true; do
    read -r -p "$prompt" value
    if [[ -n "${value// }" ]]; then
      printf '%s\n' "$value"
      return 0
    fi
    warn "This value is required."
  done
}

check_ssh_and_gpu() {
  local ssh_command="$1"
  local timeout="$2"
  python3 - "$ssh_command" "$timeout" <<'PY'
import shlex
import subprocess
import sys

ssh_command = sys.argv[1]
timeout = int(sys.argv[2])
try:
    args = shlex.split(ssh_command)
except ValueError as exc:
    raise SystemExit(f"Invalid SSH command: {exc}")
if not args or args[0].split("/")[-1] != "ssh":
    raise SystemExit("SSH command must start with ssh")
base = [
    args[0],
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={timeout}",
    "-o", "StrictHostKeyChecking=accept-new",
    *args[1:],
]
remote = "LC_ALL=C command -v nvidia-smi >/dev/null && nvidia-smi -L"
proc = subprocess.run(
    [*base, remote],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=timeout + 15,
    check=False,
)
if proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
    raise SystemExit(f"Remote GPU check failed: {detail}")
if "GPU " not in proc.stdout:
    raise SystemExit("Remote nvidia-smi returned no GPU lines")
print(proc.stdout.strip())
PY
}

write_config() {
  local path="$1"
  local ssh_command="$2"
  local webhook_url="$3"
  local timeout="$4"
  local first_run_sends="$5"
  local max_processes="$6"
  python3 - "$path" "$ssh_command" "$webhook_url" "$timeout" "$first_run_sends" "$max_processes" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
config = {
    "ssh_command": sys.argv[2],
    "ssh_timeout_seconds": int(sys.argv[4]),
    "webhook_url": sys.argv[3],
    "first_run_sends": sys.argv[5] == "true",
    "max_processes_in_card": int(sys.argv[6]),
}
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

render_service() {
  local install_dir="$1"
  local target="$2"
  python3 - "$SCRIPT_DIR/systemd/gpu-sentinel.service.in" "$target" "$install_dir" <<'PY'
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
target = Path(sys.argv[2])
install_dir = sys.argv[3]
target.write_text(template.replace("@INSTALL_DIR@", install_dir), encoding="utf-8")
PY
}

main() {
  say "GPU Sentinel installer"
  say "This installs a user-level systemd timer and stores secrets only in config.json."
  say ""

  need_command python3
  need_command ssh
  need_command systemctl
  python3 - <<'PY' >/dev/null || die "Python sqlite3 module is unavailable"
import sqlite3
PY
  systemctl --user list-timers >/dev/null || die "systemd --user is not available for this session"

  local install_dir
  install_dir="$(ask_install_dir)"

  say ""
  say "Feishu webhook guide:"
  say "1. Open a Feishu group."
  say "2. Open group settings, then Bots."
  say "3. Add Custom Bot and copy its webhook URL."
  say "4. Official guide: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot"
  say ""

  local ssh_command
  local webhook_url
  local timeout
  local max_processes
  local first_run_sends
  ssh_command="$(ask_required "SSH command to reach the GPU host, for example 'ssh user@host' or 'ssh -J jump user@host': ")"
  webhook_url="$(ask_required "Feishu bot webhook URL: ")"
  if [[ ! "$webhook_url" =~ ^https?:// ]]; then
    die "Webhook URL must start with http:// or https://"
  fi
  read -r -p "SSH timeout seconds [18]: " timeout
  timeout="${timeout:-18}"
  [[ "$timeout" =~ ^[0-9]+$ ]] || die "SSH timeout must be an integer"
  read -r -p "Max GPU processes shown in one card [50]: " max_processes
  max_processes="${max_processes:-50}"
  [[ "$max_processes" =~ ^[0-9]+$ ]] || die "Max process count must be an integer"
  if ask_yes_no "Send a Feishu notification for the first successful baseline?" "y"; then
    first_run_sends="true"
  else
    first_run_sends="false"
  fi

  say ""
  say "Checking remote GPU access..."
  check_ssh_and_gpu "$ssh_command" "$timeout"

  mkdir -p "$install_dir"
  install -m 755 "$SCRIPT_DIR/gpu_sentinel.py" "$install_dir/gpu_sentinel.py"
  install -m 755 "$SCRIPT_DIR/install.sh" "$install_dir/install.sh"
  install -m 755 "$SCRIPT_DIR/uninstall.sh" "$install_dir/uninstall.sh"
  install -m 644 "$SCRIPT_DIR/config.example.json" "$install_dir/config.example.json"
  write_config "$install_dir/config.json" "$ssh_command" "$webhook_url" "$timeout" "$first_run_sends" "$max_processes"

  mkdir -p "$HOME/.config/systemd/user"
  local service_file="$HOME/.config/systemd/user/$SERVICE_NAME.service"
  local timer_file="$HOME/.config/systemd/user/$SERVICE_NAME.timer"
  systemctl --user stop "$SERVICE_NAME.timer" >/dev/null 2>&1 || true
  systemctl --user stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
  render_service "$install_dir" "$service_file"
  install -m 644 "$SCRIPT_DIR/systemd/gpu-sentinel.timer" "$timer_file"

  say ""
  say "Running a dry-run collection..."
  python3 "$install_dir/gpu_sentinel.py" --dry-run

  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME.timer"

  say ""
  say "Installed successfully."
  say "Install path: $install_dir"
  say "Config: $install_dir/config.json"
  say "Timer: systemctl --user status $SERVICE_NAME.timer"
  say "Preview card: python3 $install_dir/gpu_sentinel.py --preview-card"
}

main "$@"
