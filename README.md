# GPU Sentinel Feishu

GPU Sentinel Feishu is a small Linux service that watches a remote NVIDIA GPU
server over SSH. It sends a Feishu interactive card only when GPU workload
changes: new GPU processes start, old GPU processes finish, or the first
baseline is created.

It stores history in SQLite, keeps secrets in a local `config.json`, and runs
as a `systemd --user` timer by default.

## Features

- Monitors any number of NVIDIA GPUs reported by `nvidia-smi`.
- Detects GPU workload changes by process, GPU binding, owner, start time, and command hash.
- Sends readable Feishu Card JSON 2.0 notifications with collapsible process command details.
- Shows GPU utilization, memory, temperature, power, CPU load, memory, uptime, and zero-util duration.
- Keeps historical snapshots in `gpu_sentinel.sqlite3`.
- Installs without root by using a user-level systemd service and timer.

## Requirements

On the machine that runs GPU Sentinel:

- Linux with `systemd --user`
- `python3` with the standard `sqlite3` module
- `ssh`

On the remote GPU server:

- SSH access from the machine running GPU Sentinel
- `nvidia-smi`
- `ps`

## Get a Feishu Webhook URL

1. Open the Feishu group that should receive alerts.
2. Open group settings.
3. Go to **Bots**.
4. Add a **Custom Bot**.
5. Copy the webhook URL.

Official documentation: [Custom bot usage guide](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot).

Keep this URL private. Anyone with the webhook URL may be able to send messages
to your group.

## Install

Clone the repository on the machine that should run the timer:

```bash
git clone https://github.com/iamwsll/gpu-sentinel-feishu.git
cd gpu-sentinel-feishu
./install.sh
```

The installer asks for:

- Install path. There is no default; you must type an explicit path.
  The path must not contain whitespace.
- SSH command, for example:
  - `ssh user@gpu-host`
  - `ssh -J jump-user@jump-host user@gpu-host`
  - `ssh -i ~/.ssh/gpu_key user@gpu-host`
- Feishu bot webhook URL.
- SSH timeout.
- Maximum process count shown in one card.
- Whether to send a first successful baseline notification.

The installer checks local dependencies, verifies remote `nvidia-smi`, writes
`config.json`, installs `gpu-sentinel.service` and `gpu-sentinel.timer`, runs a
dry-run collection, and enables the timer.

## Common Commands

Check the timer:

```bash
systemctl --user status gpu-sentinel.timer
systemctl --user list-timers --all | grep gpu-sentinel
```

Check recent logs:

```bash
journalctl --user -u gpu-sentinel.service -n 50 --no-pager
```

Send a preview card without updating the SQLite baseline:

```bash
python3 /your/install/path/gpu_sentinel.py --preview-card
```

Collect and compare without sending or updating state:

```bash
python3 /your/install/path/gpu_sentinel.py --dry-run
```

## Configuration

The installer writes `/your/install/path/config.json`:

```json
{
  "ssh_command": "ssh user@gpu-host",
  "ssh_timeout_seconds": 18,
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_ME",
  "first_run_sends": true,
  "max_processes_in_card": 50
}
```

`config.json` is intentionally ignored by git. Do not commit real webhook URLs,
SSH usernames, hostnames, or private deployment details.

## Uninstall

Run:

```bash
/your/install/path/uninstall.sh
```

The uninstaller stops and disables the user timer, removes the user-level
systemd service and timer files, and asks whether to delete the install
directory with all local data, including `config.json` and
`gpu_sentinel.sqlite3`.

## Troubleshooting

If `systemctl --user` does not work, log in as the target user and confirm the
user systemd manager is running:

```bash
systemctl --user list-timers
```

If SSH checks fail, first make sure this command works without a password:

```bash
ssh user@gpu-host 'nvidia-smi -L'
```

If Feishu cards do not arrive, run:

```bash
python3 /your/install/path/gpu_sentinel.py --preview-card
```

If the preview command fails, check whether the webhook URL is correct and
whether the custom bot is still enabled in the Feishu group.

## Security Notes

- `config.json` is written with `0600` permissions.
- SQLite history is stored locally and ignored by git.
- The service runs as your user, not as root.
- The project does not need your SSH private key; it only runs the SSH command
  you provide.

## License

MIT
