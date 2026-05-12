#!/usr/bin/env python3
"""GPU workload sentinel with Feishu card notifications.

The script runs on a Linux host. It SSHes to the GPU server, collects a
structured snapshot, compares it with the previous successful snapshot, and
sends a Feishu bot card only when the GPU workload changed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import sqlite3
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("GPU_SENTINEL_CONFIG", str(BASE_DIR / "config.json")))
DB_PATH = Path(os.environ.get("GPU_SENTINEL_DB", str(BASE_DIR / "gpu_sentinel.sqlite3")))


DEFAULT_CONFIG: dict[str, Any] = {
    "ssh_command": "",
    "ssh_timeout_seconds": 18,
    "webhook_url": "",
    "first_run_sends": True,
    "max_processes_in_card": 50,
}


class SentinelError(RuntimeError):
    """A collector or delivery failure that should be reported clearly."""


def now_local() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SentinelError(f"Missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    config = dict(DEFAULT_CONFIG)
    config.update(raw)
    if not config.get("ssh_command"):
        raise SentinelError("ssh_command is empty in config.json")
    if not config.get("webhook_url"):
        raise SentinelError("webhook_url is empty in config.json")
    return config


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def ssh_base_args(config: dict[str, Any]) -> list[str]:
    try:
        args = shlex.split(str(config["ssh_command"]))
    except ValueError as exc:
        raise SentinelError(f"Invalid ssh_command: {exc}") from exc
    if not args:
        raise SentinelError("ssh_command is empty in config.json")
    if Path(args[0]).name != "ssh":
        raise SentinelError("ssh_command must start with ssh")
    return [
        args[0],
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(config['ssh_timeout_seconds'])}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        *args[1:],
    ]


def ssh(config: dict[str, Any], remote_command: str) -> str:
    args = [*ssh_base_args(config), remote_command]
    proc = run_command(args, timeout=int(config["ssh_timeout_seconds"]) + 10)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise SentinelError(f"SSH command failed: {detail}")
    return proc.stdout


def parse_csv_lines(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in csv.reader(line for line in text.splitlines() if line.strip()):
        rows.append([cell.strip() for cell in row])
    return rows


def parse_int(value: str, default: int = 0) -> int:
    value = value.strip()
    if value in {"", "-", "[Not Supported]", "N/A"}:
        return default
    match = re.search(r"-?\d+", value)
    return int(match.group(0)) if match else default


def parse_float(value: str, default: float = 0.0) -> float:
    value = value.strip()
    if value in {"", "-", "[Not Supported]", "N/A"}:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else default


def parse_gpu_rows(raw: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for row in parse_csv_lines(raw):
        if len(row) < 8:
            continue
        index, uuid, name, util, mem_used, mem_total, temp, power = row[:8]
        gpus.append(
            {
                "index": parse_int(index),
                "uuid": uuid,
                "name": name,
                "util_gpu_pct": parse_int(util),
                "memory_used_mb": parse_int(mem_used),
                "memory_total_mb": parse_int(mem_total),
                "temperature_c": parse_int(temp),
                "power_w": round(parse_float(power), 1),
            }
        )
    return sorted(gpus, key=lambda item: item["index"])


def parse_compute_rows(raw: str, uuid_to_index: dict[str, int]) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    for row in parse_csv_lines(raw):
        if len(row) < 4:
            continue
        pid, gpu_uuid, process_name, used_memory = row[:4]
        if not pid.strip().isdigit():
            continue
        apps.append(
            {
                "pid": int(pid),
                "gpu_uuid": gpu_uuid,
                "gpu_index": uuid_to_index.get(gpu_uuid),
                "process_name": process_name,
                "used_memory_mb": parse_int(used_memory),
            }
        )
    return sorted(apps, key=lambda item: (item.get("gpu_index") is None, item.get("gpu_index", 999), item["pid"]))


def parse_ps_rows(raw: str) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 8)
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        pid_s, user, etimes_s = parts[:3]
        lstart = " ".join(parts[3:8])
        stat = parts[8].split(None, 1)[0] if parts[8] else ""
        command = parts[8].split(None, 1)[1] if len(parts[8].split(None, 1)) > 1 else ""
        rows[int(pid_s)] = {
            "user": user,
            "etimes_seconds": parse_int(etimes_s),
            "started_at": lstart,
            "stat": stat,
            "command": command,
            "command_sha1": hashlib.sha1(command.encode("utf-8", errors="replace")).hexdigest()[:12],
        }
    return rows


def collect_system(config: dict[str, Any]) -> dict[str, Any]:
    command = "export LC_ALL=C; hostname; cat /proc/loadavg; cat /proc/uptime; nproc; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; df -h / /home /data2 2>/dev/null; who | wc -l"
    raw = ssh(config, command)
    lines = raw.splitlines()
    system: dict[str, Any] = {
        "hostname": lines[0].strip() if lines else "unknown",
        "raw": raw.strip(),
    }
    if len(lines) >= 2:
        load_parts = lines[1].split()
        if len(load_parts) >= 3:
            system["load_avg"] = load_parts[:3]
    if len(lines) >= 3:
        uptime_seconds = parse_float(lines[2].split()[0])
        system["uptime_seconds"] = int(uptime_seconds)
    if len(lines) >= 4:
        system["cpu_cores"] = parse_int(lines[3])
    meminfo: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            meminfo[key] = parse_int(rest) // 1024
    if meminfo:
        system["memory_mb"] = meminfo
    if lines:
        last = lines[-1].strip()
        if last.isdigit():
            system["login_users"] = int(last)
    return system


def collect_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    collected_at = now_local()
    gpu_query = (
        "LC_ALL=C nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,"
        "memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits"
    )
    compute_query = (
        "LC_ALL=C nvidia-smi --query-compute-apps=pid,gpu_uuid,process_name,used_memory "
        "--format=csv,noheader,nounits"
    )
    raw_gpu = ssh(config, gpu_query)
    gpus = parse_gpu_rows(raw_gpu)
    if not gpus:
        raise SentinelError("No NVIDIA GPUs were collected from nvidia-smi")

    uuid_to_index = {gpu["uuid"]: gpu["index"] for gpu in gpus}
    raw_compute = ssh(config, compute_query)
    apps = parse_compute_rows(raw_compute, uuid_to_index)

    if apps:
        pid_arg = ",".join(str(app["pid"]) for app in apps)
        ps_raw = ssh(config, f"LC_ALL=C ps -p {pid_arg} -o pid=,user=,etimes=,lstart=,stat=,args=")
        ps_rows = parse_ps_rows(ps_raw)
        for app in apps:
            app.update(ps_rows.get(app["pid"], {}))

    return {
        "schema": 1,
        "collected_at": collected_at,
        "collector_host": socket.gethostname(),
        "system": collect_system(config),
        "gpus": gpus,
        "compute_apps": apps,
    }


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.touch(mode=0o600, exist_ok=True)
    DB_PATH.chmod(0o600)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            collected_at TEXT,
            changed INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            new_processes INTEGER NOT NULL DEFAULT 0,
            ended_processes INTEGER NOT NULL DEFAULT 0,
            feishu_json TEXT,
            result_json TEXT NOT NULL,
            snapshot_json TEXT,
            error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_collected_at ON runs(collected_at)")
    secure_db_permissions()
    return conn


def secure_db_permissions() -> None:
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        try:
            if path.exists():
                path.chmod(0o600)
        except OSError:
            pass


def load_previous() -> dict[str, Any] | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT snapshot_json FROM runs WHERE snapshot_json IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def save_state(snapshot: dict[str, Any], result: dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (
                created_at, collected_at, changed, reason, new_processes,
                ended_processes, feishu_json, result_json, snapshot_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                now_local(),
                snapshot.get("collected_at"),
                1 if result.get("changed") else 0,
                result.get("reason"),
                int(result.get("new_processes", 0)),
                int(result.get("ended_processes", 0)),
                json.dumps(result.get("feishu"), ensure_ascii=False, sort_keys=True),
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )


def save_error(error: dict[str, Any]) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    created_at, collected_at, changed, reason, new_processes,
                    ended_processes, feishu_json, result_json, snapshot_json, error
                ) VALUES (?, NULL, 0, ?, 0, 0, NULL, ?, NULL, ?)
                """,
                (
                    now_local(),
                    error.get("reason", "collector_error"),
                    json.dumps(error, ensure_ascii=False, sort_keys=True),
                    str(error.get("error", "")),
                ),
            )
    except Exception:
        pass


def process_key(app: dict[str, Any]) -> tuple[Any, ...]:
    return (
        app.get("gpu_index"),
        app.get("pid"),
        app.get("user", ""),
        app.get("process_name", ""),
        app.get("started_at", ""),
        app.get("command_sha1", ""),
    )


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def annotate_zero_util_duration(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    collected_at = parse_timestamp(snapshot.get("collected_at"))
    previous_snapshot = previous or {}
    previous_gpus = {gpu.get("index"): gpu for gpu in previous_snapshot.get("gpus", [])}
    previous_collected_at = previous_snapshot.get("collected_at")

    for gpu in snapshot.get("gpus", []):
        if gpu.get("util_gpu_pct") != 0:
            gpu["zero_util_since"] = None
            gpu["zero_util_seconds"] = 0
            continue

        old = previous_gpus.get(gpu.get("index"))
        if old and old.get("util_gpu_pct") == 0:
            zero_since = old.get("zero_util_since") or previous_collected_at or snapshot.get("collected_at")
        else:
            zero_since = snapshot.get("collected_at")

        gpu["zero_util_since"] = zero_since
        since_at = parse_timestamp(zero_since)
        if collected_at and since_at:
            gpu["zero_util_seconds"] = max(0, int((collected_at - since_at).total_seconds()))
        else:
            gpu["zero_util_seconds"] = 0


def compare_snapshots(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if previous is None:
        return {
            "changed": bool(config.get("first_run_sends", True)),
            "reason": "first_successful_snapshot",
            "new_processes": current["compute_apps"],
            "ended_processes": [],
            "changed_processes": [],
            "gpu_notices": [],
        }

    prev_by_key = {process_key(app): app for app in previous.get("compute_apps", [])}
    curr_by_key = {process_key(app): app for app in current.get("compute_apps", [])}
    new_keys = sorted(set(curr_by_key) - set(prev_by_key))
    ended_keys = sorted(set(prev_by_key) - set(curr_by_key))

    changed = bool(new_keys or ended_keys)
    return {
        "changed": changed,
        "reason": "diff" if changed else "unchanged",
        "new_processes": [curr_by_key[key] for key in new_keys],
        "ended_processes": [prev_by_key[key] for key in ended_keys],
        "changed_processes": [],
        "gpu_notices": [],
    }


def fmt_duration(seconds: Any) -> str:
    try:
        seconds_i = int(seconds)
    except (TypeError, ValueError):
        return "-"
    days, rem = divmod(seconds_i, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def mb_to_gib(mb: int | float) -> str:
    return f"{float(mb) / 1024:.1f} GiB"


def short_command(command: str, limit: int = 52) -> str:
    command = " ".join(command.split())
    if len(command) <= limit:
        return command
    return command[: limit - 1] + "…"


def apps_for_gpu(apps: list[dict[str, Any]], gpu_index: int) -> list[dict[str, Any]]:
    return [app for app in apps if app.get("gpu_index") == gpu_index]


def owner_text(gpu_apps: list[dict[str, Any]]) -> str:
    if not gpu_apps:
        return "idle"
    owners = sorted({app.get("user") or "?" for app in gpu_apps})
    return ", ".join(owners)


def workload_text(gpu_apps: list[dict[str, Any]]) -> str:
    if not gpu_apps:
        return "0 jobs"
    users = owner_text(gpu_apps)
    return f"{len(gpu_apps)} job(s) by {users}"


def util_text(gpu: dict[str, Any]) -> str:
    util = int(gpu.get("util_gpu_pct", 0))
    if util == 0:
        return f"0%\n为 0 已 {fmt_duration(gpu.get('zero_util_seconds', 0))}"
    return f"{util}%"


def util_inline_text(gpu: dict[str, Any]) -> str:
    util = int(gpu.get("util_gpu_pct", 0))
    if util == 0:
        return f"0% (为 0 已 {fmt_duration(gpu.get('zero_util_seconds', 0))})"
    return f"{util}%"


def process_line(app: dict[str, Any]) -> str:
    command = short_command(app.get("command") or app.get("process_name", "-"), 72)
    runtime = fmt_duration(app.get("etimes_seconds"))
    started = app.get("started_at", "-")
    return (
        f"GPU{app.get('gpu_index', '?')} PID {app.get('pid')} "
        f"{app.get('user', '?')} {app.get('used_memory_mb', 0)} MiB "
        f"run {runtime} start {started} | {command}"
    )


def system_summary(system: dict[str, Any]) -> str:
    mem = system.get("memory_mb", {})
    mem_text = "-"
    if mem:
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used = max(total - avail, 0)
        mem_text = f"{used / 1024:.1f}/{total / 1024:.1f} GiB used, {avail / 1024:.1f} GiB available"
    load = " / ".join(system.get("load_avg", [])) or "-"
    cores = system.get("cpu_cores", "-")
    uptime = fmt_duration(system.get("uptime_seconds"))
    users = system.get("login_users", "-")
    return f"host {system.get('hostname', '-')}; CPU load 1m/5m/15m {load} on {cores} cores; uptime {uptime}; login users {users}; memory {mem_text}"


def compact_system_summary(system: dict[str, Any]) -> str:
    mem = system.get("memory_mb", {})
    mem_text = "-"
    if mem:
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used = max(total - avail, 0)
        mem_text = f"{used / 1024:.1f}/{total / 1024:.1f} GiB"
    load_values = system.get("load_avg", [])[:3]
    load = " / ".join(load_values) or "-"
    cores = system.get("cpu_cores")
    cpu_note = ""
    if cores:
        one_min_load = parse_float(str(load_values[0])) if load_values else 0.0
        cpu_note = f" ({cores} cores, 1m ~= {one_min_load / max(int(cores), 1) * 100:.0f}% of total cores)"
    uptime = fmt_duration(system.get("uptime_seconds"))
    return f"{system.get('hostname', '-')}\nCPU load 1m/5m/15m: {load}{cpu_note}\nup {uptime} | mem {mem_text}"


def markdown(content: str) -> dict[str, str]:
    return {"tag": "markdown", "content": content}


def gpu_card_element(gpu: dict[str, Any], apps: list[dict[str, Any]]) -> dict[str, Any]:
    gpu_apps = apps_for_gpu(apps, gpu["index"])
    memory = f"{mb_to_gib(gpu['memory_used_mb'])} / {mb_to_gib(gpu['memory_total_mb'])}"
    thermal = f"{gpu['temperature_c']} C / {gpu['power_w']:.1f} W"
    return markdown(
        f"**GPU {gpu['index']}**\n"
        f"**Workload**: {workload_text(gpu_apps)} | **Util**: {util_inline_text(gpu)}\n"
        f"**Memory**: {memory} | **Temp / Power**: {thermal}"
    )


def current_workload_elements(apps: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not apps:
        return [markdown("**Current workload**\n当前没有 GPU compute 进程。")]

    elements = [markdown("**Current workload**")]
    shown = 0
    for gpu_index in sorted({app.get("gpu_index") for app in apps}):
        gpu_apps = [app for app in apps if app.get("gpu_index") == gpu_index]
        if not gpu_apps:
            continue
        lines = [f"**GPU {gpu_index}**"]
        for app in gpu_apps:
            if shown >= limit:
                elements.append(markdown(f"还有 {len(apps) - shown} 个进程，详见本机 SQLite。"))
                return elements
            lines.append(f"- PID {app.get('pid')} | {app.get('user', '?')} | {mb_to_gib(app.get('used_memory_mb', 0))} | {fmt_duration(app.get('etimes_seconds'))}")
            shown += 1
        elements.append(markdown("\n".join(lines)))
    return elements


def command_detail(app: dict[str, Any]) -> str:
    command = " ".join((app.get("command") or app.get("process_name") or "-").split())
    return command


def markdown_code_block(content: str) -> str:
    safe = content.replace("```", "` ` `")
    return f"```text\n{safe}\n```"


def process_name_detail_elements(apps: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not apps:
        return []

    elements: list[dict[str, Any]] = [markdown("**PID process names**")]
    for app in apps[:limit]:
        command = command_detail(app)
        details = (
            f"**PID {app.get('pid')} | GPU{app.get('gpu_index', '?')} | {app.get('user', '?')} | {app.get('process_name') or '-'}**\n"
            f"**Command**\n{markdown_code_block(command)}"
        )
        elements.append(markdown(details))
    if len(apps) > limit:
        elements.append(markdown(f"还有 {len(apps) - limit} 个进程，完整数据保存在本机 SQLite。"))
    return elements


def collapsible_details(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "element_id": "gpu_sentinel_details",
        "expanded": False,
        "header": {
            "title": {"tag": "plain_text", "content": "GPU details"},
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "padding": "8px 8px 8px 8px",
        "vertical_spacing": "8px",
        "elements": elements,
    }


def trigger_reason_line(label: str, app: dict[str, Any]) -> str:
    process_name = short_command(app.get("process_name") or app.get("command") or "-", 48)
    return (
        f"**【{label}】** GPU{app.get('gpu_index', '?')} | "
        f"{app.get('user', '?')} | PID {app.get('pid')} | "
        f"{process_name} | {mb_to_gib(app.get('used_memory_mb', 0))}"
    )


def trigger_reason_text(diff: dict[str, Any], limit: int) -> str:
    lines: list[str] = []
    shown = 0
    for app in diff["new_processes"]:
        if shown >= limit:
            break
        lines.append(trigger_reason_line("开始运行", app))
        shown += 1
    for app in diff["ended_processes"]:
        if shown >= limit:
            break
        lines.append(trigger_reason_line("结束运行", app))
        shown += 1
    remaining = len(diff["new_processes"]) + len(diff["ended_processes"]) - shown
    if remaining > 0:
        lines.append(f"还有 {remaining} 条触发原因，展开下方详情查看完整进程信息。")
    return "\n".join(lines) if lines else "本次是格式预览或基线初始化。"


def build_card(snapshot: dict[str, Any], diff: dict[str, Any], config: dict[str, Any], preview: bool = False) -> dict[str, Any]:
    new_count = len(diff["new_processes"])
    ended_count = len(diff["ended_processes"])
    if preview:
        title = "GPU 监控格式预览"
        template = "purple"
    elif diff["reason"] == "first_successful_snapshot":
        title = "GPU monitor baseline initialized"
        template = "blue"
    elif ended_count and not new_count:
        title = "GPU workload finished"
        template = "red"
    elif new_count and not ended_count:
        title = "GPU workload started"
        template = "green"
    elif new_count and ended_count:
        title = "GPU workload changed"
        template = "yellow"
    else:
        title = "GPU state changed"
        template = "wathet"

    apps = snapshot["compute_apps"]
    max_processes = int(config["max_processes_in_card"])
    diff_text = f"+{new_count} new / -{ended_count} finished"
    detail_elements: list[dict[str, Any]] = [
        markdown(
            f"**Collected**\n{snapshot['collected_at']}\n\n"
            f"**Diff**\n{diff_text}\n\n"
            f"**System**\n{compact_system_summary(snapshot['system'])}"
        ),
        {"tag": "hr"},
        markdown("**GPU overview**"),
    ]
    for gpu in snapshot["gpus"]:
        detail_elements.append(gpu_card_element(gpu, apps))

    if apps:
        detail_elements.extend(current_workload_elements(apps, max_processes))
        detail_elements.extend(process_name_detail_elements(apps, max_processes))
    detail_elements.append(markdown("*完整历史和原始快照保存在本机 gpu_sentinel.sqlite3。*"))

    elements: list[dict[str, Any]] = [
        markdown(f"**触发原因**\n{trigger_reason_text(diff, max_processes)}"),
        {"tag": "hr"},
        collapsible_details(detail_elements),
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "fill"},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "body": {"elements": elements},
        },
    }


def send_feishu(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"raw": raw}
    if result.get("code", result.get("StatusCode", 0)) not in {0, None}:
        raise SentinelError(f"Feishu webhook rejected the card: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="collect and compare, but do not send or update state")
    parser.add_argument("--preview-card", action="store_true", help="send a formatting preview without updating state")
    args = parser.parse_args()

    config = load_config()
    previous = load_previous()
    snapshot = collect_snapshot(config)
    annotate_zero_util_duration(snapshot, previous)
    diff = compare_snapshots(previous, snapshot, config)

    send_result: dict[str, Any] | None = None
    if args.preview_card:
        send_result = send_feishu(config["webhook_url"], build_card(snapshot, diff, config, preview=True))
    elif diff["changed"] and not args.dry_run:
        send_result = send_feishu(config["webhook_url"], build_card(snapshot, diff, config))

    result = {
        "changed": diff["changed"],
        "reason": diff["reason"],
        "new_processes": len(diff["new_processes"]),
        "ended_processes": len(diff["ended_processes"]),
        "gpu_notices": len(diff["gpu_notices"]),
        "feishu": send_result,
    }
    if not args.dry_run and not args.preview_card:
        save_state(snapshot, result)

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        error = {"changed": False, "reason": "collector_error", "error": str(exc), "at": now_local()}
        save_error(error)
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
