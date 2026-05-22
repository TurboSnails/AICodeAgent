#!/usr/bin/env python3
"""Claude CLI subprocess 进度 — 写入 phase_status / coding_cli.log 供 Web 轮询。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from utils.logging_config import get_logger
from utils.phase_status import write_phase_status

logger = get_logger(__name__)

CLI_LOG_NAME = "coding_cli.log"


def cli_log_path(workspace: Path) -> Path:
    return workspace / CLI_LOG_NAME


def append_cli_log(workspace: Path, line: str, *, stream: str = "stderr") -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        with cli_log_path(workspace).open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{stream}] {line.rstrip()}\n")
    except OSError as e:
        logger.debug("append_cli_log failed: %s", e)


def tail_cli_log(workspace: Path, max_lines: int = 10) -> str:
    path = cli_log_path(workspace)
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def update_cli_progress(
    workspace: Path,
    *,
    state: str = "coding",
    pid: int = 0,
    elapsed_sec: int = 0,
    timeout_sec: int = 0,
    attempt: int = 1,
    max_attempts: int = 1,
    running: bool = True,
    stderr_hint: str = "",
    stdout_len: int = 0,
    exit_code: Optional[int] = None,
) -> None:
    alive = is_pid_alive(pid) if running else False
    tail = tail_cli_log(workspace, 8)
    if stderr_hint and stderr_hint not in tail:
        tail = (stderr_hint + "\n" + tail).strip() if tail else stderr_hint

    if running and alive:
        status_label = "running"
        detail = (
            f"Claude CLI 运行中（PID {pid}，已 {elapsed_sec}s / 上限 {timeout_sec}s，"
            f"第 {attempt}/{max_attempts} 次）"
        )
    elif running and not alive and pid > 0:
        status_label = "exited_wait"
        detail = f"CLI 进程已结束，等待收尾…（已 {elapsed_sec}s）"
    elif exit_code is not None:
        status_label = "done" if exit_code == 0 else "failed"
        detail = f"CLI 已结束 exit={exit_code}（耗时 {elapsed_sec}s，stdout {stdout_len} 字符）"
    else:
        status_label = "starting"
        detail = f"正在启动 Claude CLI…（第 {attempt}/{max_attempts} 次）"

    extra: dict[str, Any] = {
        "cli_transport": "subprocess_cli",
        "cli_pid": pid,
        "cli_running": alive,
        "cli_status": status_label,
        "elapsed_sec": elapsed_sec,
        "timeout_sec": timeout_sec,
        "claude_attempt": attempt,
        "claude_max_attempts": max_attempts,
        "stdout_len": stdout_len,
        "waiting_ai": running,
    }
    if tail:
        extra["cli_log_tail"] = _sanitize_cli_tail_for_web(tail[-1200:])
    if exit_code is not None:
        extra["cli_exit_code"] = exit_code

    write_phase_status(workspace, state, detail, extra=extra)


def _sanitize_cli_tail_for_web(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("cmd:") and len(line) > 240:
            line = line[:200].rstrip() + f" … (truncated, total {len(line)} chars)"
        lines.append(line)
    return "\n".join(lines)


def format_cmd_for_log(cmd: list[str]) -> str:
    """日志中省略 -p 后的长 prompt，避免 Web 展示与 phase_status 膨胀。"""
    parts: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ("-p", "--print") and i + 1 < len(cmd):
            prompt = cmd[i + 1]
            parts.extend([cmd[i], f"<prompt {len(prompt)} chars>"])
            i += 2
            continue
        parts.append(cmd[i])
        i += 1
    return " ".join(parts)


def init_cli_log(workspace: Path, cmd: list[str]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    path = cli_log_path(workspace)
    header = (
        f"=== Claude CLI {datetime.now().isoformat()} ===\n"
        f"cmd: {format_cmd_for_log(cmd)}\n"
        "--- stderr (streaming) ---\n"
    )
    try:
        path.write_text(header, encoding="utf-8")
    except OSError as e:
        logger.warning("init_cli_log failed: %s", e)
