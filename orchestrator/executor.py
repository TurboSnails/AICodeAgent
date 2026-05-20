#!/usr/bin/env python3
"""
Serial Task Executor
- 文件锁确保单进程运行
- SQLite 队列 dequeue
- 为每个任务创建独立工作区
"""

import fcntl
import os
import sys
import time
from pathlib import Path

from state_machine import init_db, get_executable_tasks, get_waiting_gates, transition, State
from orchestrator import process_task

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "AICodeAgent" / "workspace"
LOCK_FILE = PROJECT_ROOT / "AICodeAgent" / "data" / "executor.lock"


def acquire_lock():
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def create_workspace(task_id: str) -> Path:
    ws = WORKSPACE_ROOT / task_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "figma").mkdir(exist_ok=True)
    (ws / "assets").mkdir(exist_ok=True)
    return ws


from datetime import datetime, timedelta

def cleanup_workspace(task_id: str, max_age_days: int = 7):
    ws = WORKSPACE_ROOT / task_id
    if not ws.exists():
        return
    # 保留日志，删除 figma 大文件
    figma_dir = ws / "figma"
    if figma_dir.exists():
        for f in figma_dir.glob("*"):
            if f.is_file():
                f.unlink()
    # 7 天后删除整个工作区
    try:
        mtime = datetime.fromtimestamp(ws.stat().st_mtime)
        if datetime.now() - mtime > timedelta(days=max_age_days):
            import shutil
            shutil.rmtree(ws, ignore_errors=True)
            print(f"[CLEANUP] Removed old workspace {task_id} (>{max_age_days} days)")
    except OSError:
        pass


TASK_TOTAL_TIMEOUT_SEC = int(os.environ.get("AGENT_TASK_TOTAL_TIMEOUT", "7200"))


def _check_stalled_tasks():
    """检查是否有非终态任务已运行超过总超时，如有则强制标记为 FAILED"""
    from datetime import datetime
    from state_machine import get_all_non_terminal_tasks, get_task_state_history
    stalled = []
    for task in get_all_non_terminal_tasks():
        history = get_task_state_history(task.task_id)
        if not history:
            continue
        # 取最近一次状态变更时间
        last_ts = history[-1]["timestamp"]
        try:
            last_dt = datetime.fromisoformat(last_ts)
            elapsed = (datetime.now() - last_dt).total_seconds()
            if elapsed > TASK_TOTAL_TIMEOUT_SEC:
                stalled.append((task.task_id, elapsed))
        except ValueError:
            continue
    for tid, elapsed in stalled:
        print(f"[Executor] Task {tid} stalled for {int(elapsed)}s, marking FAILED (total timeout {TASK_TOTAL_TIMEOUT_SEC}s)")
        transition(tid, State.FAILED, f"total timeout exceeded ({int(elapsed)}s)")


def run_loop():
    init_db()
    print(f"[Executor] Serial task executor started (task_total_timeout={TASK_TOTAL_TIMEOUT_SEC}s)")
    while True:
        lock_fd = acquire_lock()
        if lock_fd is None:
            print("[Executor] Another instance is running, waiting...")
            time.sleep(10)
            continue
        try:
            # 健康检查：清理超时任务
            _check_stalled_tasks()

            tasks = get_executable_tasks(limit=1)
            if not tasks:
                # 检查 L2 超时
                from datetime import datetime
                gates = get_waiting_gates()
                for gate in gates:
                    if gate.gate_deadline and datetime.fromisoformat(gate.gate_deadline) < datetime.now():
                        transition(gate.task_id, State.CANCELLED, "L2 gate timeout 24h")
                time.sleep(5)
                continue
            task = tasks[0]
            print(f"[Executor] Processing task {task.task_id}")
            try:
                process_task(task)
            except Exception as e:
                print(f"[Executor] Task {task.task_id} failed: {e}")
                transition(task.task_id, State.FAILED, str(e))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


if __name__ == "__main__":
    run_loop()
