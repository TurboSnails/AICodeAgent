#!/usr/bin/env python3
"""
V4 执行器入口 — AgentEngine 集成版
替换旧 executor.py 的 while 循环，使用显式 PhaseHandler 注册表。

特性：
- 显式状态处理器注册表（替代硬编码 while 循环）
- 服务注入（依赖反转）
- 信号优雅关闭
- 配置预校验
- 环境快照 + 恢复
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from utils.paths import DATA_DIR, PROJECT_ROOT, WORKSPACE_ROOT

from utils.config_loader import cfg_int
from utils.logging_config import get_logger
from engine.state_machine import (
    DB_FILE,
    State,
    Task,
    get_executable_tasks,
    get_task,
    init_db,
    save_task,
    transition,
)
from engine.config_validator import validate_config
from engine.core import AgentEngine
from engine.exceptions import AgentFatalError
from phases import (
    ArchitectPlanningHandler,
    ArchitectReviewHandler,
    BuildingHandler,
    CodexReviewHandler,
    ConsensusHandler,
    CorrectingHandler,
    CreatingPRHandler,
    CodingHandler,
    DebateHandler,
    DirectAnswerHandler,
    GitCommittingHandler,
    NotifyingHandler,
    PlanningHandler,
    RedTeamReviewHandler,
    RequirementReviewHandler,
    SelfReviewHandler,
)
from services.ai_client import AIClient
from services.build_service import BuildService
from services.git_service import GitService
from services.notification_service import NotificationService
from services.tencent_memory_service import get_memory_service
from utils.memory_context import write_memory_recall_file
from utils.task_cancel import clear_active_task, is_task_cancelled, set_active_task

logger = get_logger(__name__)

LOCK_FILE = DATA_DIR / "executor.lock"
CURRENT_TASK_FILE = DATA_DIR / "executor.current_task"

_shutdown_requested = False
_current_task_id: str = ""


def _signal_handler(signum, _frame):
    global _shutdown_requested
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _shutdown_requested = True


def acquire_lock() -> int | None:
    """获取文件锁，确保单进程运行"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
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


# ------------------------------------------------------------------
# V4 核心：构建 AgentEngine 并注册所有 PhaseHandler
# ------------------------------------------------------------------


def build_engine() -> AgentEngine:
    """构建并配置 AgentEngine，注册所有阶段处理器"""

    # 1. 创建服务实例
    ai_client = AIClient()
    build_service = BuildService()
    git_service = GitService()
    notification_service = NotificationService()

    # 2. 创建引擎
    engine = AgentEngine(workspace_root=WORKSPACE_ROOT)

    # 3. 注册阶段处理器
    engine.register(State.PLANNING, PlanningHandler(
        ai_client=ai_client,
        notification_service=notification_service,
    ))
    engine.register(State.DEBATING, DebateHandler(
        ai_client=ai_client,
    ))
    engine.register(State.CONSENSUS, ConsensusHandler(
        ai_client=ai_client,
        notification_service=notification_service,
    ))
    engine.register(State.ARCHITECT_PLANNING, ArchitectPlanningHandler(
        ai_client=ai_client,
        notification_service=notification_service,
    ))
    engine.register(State.DIRECT_ANSWER, DirectAnswerHandler(
        ai_client=ai_client,
        notification_service=notification_service,
    ))
    engine.register(State.CODING, CodingHandler(
        ai_client=ai_client,
        git_service=git_service,
    ))
    engine.register(State.BUILDING, BuildingHandler(
        build_service=build_service,
    ))
    engine.register(State.SELF_REVIEW, SelfReviewHandler(
        ai_client=ai_client,
    ))
    engine.register(State.CODEX_REVIEW, CodexReviewHandler(
        ai_client=ai_client,
    ))
    engine.register(State.ARCHITECT_REVIEW, ArchitectReviewHandler(
        ai_client=ai_client,
    ))
    engine.register(State.RED_TEAM_REVIEW, RedTeamReviewHandler(
        ai_client=ai_client,
    ))
    engine.register(State.REQUIREMENT_REVIEW, RequirementReviewHandler(
        ai_client=ai_client,
    ))
    engine.register(State.CORRECTING, CorrectingHandler())
    engine.register(State.GIT_COMMITTING, GitCommittingHandler(
        git_service=git_service,
    ))
    engine.register(State.CREATING_PR, CreatingPRHandler(
        git_service=git_service,
    ))
    engine.register(State.NOTIFYING, NotifyingHandler(
        notification_service=notification_service,
    ))

    logger.info("AgentEngine built with %d handlers", len(engine.list_registered()))
    return engine


# ------------------------------------------------------------------
# 任务处理（带环境快照和恢复）
# ------------------------------------------------------------------


def _snapshot_git_state() -> tuple[str, list[tuple[str, str]]]:
    """返回 (当前分支, git status 快照)"""
    git = GitService()
    current_branch = git.get_current_branch()
    # 简化快照：只记录分支名
    return current_branch, []


def _has_uncommitted_work() -> bool:
    """检测工作区是否有未提交修改"""
    git = GitService()
    code, out, _ = git._run_cmd(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    if code == 0:
        for line in out.splitlines():
            if len(line) >= 3 and line[:2] != "??":
                return True
    return False


def _restore_environment(task: Task, original_branch: str) -> None:
    """任务结束后恢复环境"""
    logger.info("Restoring environment after task %s", task.task_id)
    git = GitService()
    base = task.base_branch or original_branch or "main"

    # 安全模式检查
    if _has_uncommitted_work():
        logger.warning("Detected uncommitted work — skipping destructive cleanup")
        return

    try:
        # 切回 base branch
        git._run_cmd(["git", "stash", "push", "--include-untracked", "-m", f"agent-cleanup-{task.task_id}"])
        git._run_cmd(["git", "checkout", base])

        # 删除 agent 分支
        if task.branch:
            _, branch_out, _ = git._run_cmd(["git", "branch", "--show-current"], capture=True)
            if branch_out.strip() != task.branch:
                git._run_cmd(["git", "branch", "-D", task.branch])

        # 恢复 tracked 文件
        git._run_cmd(["git", "checkout", "--", "."])

        # 清理 stash
        git._run_cmd(["git", "stash", "drop"])
    except Exception as e:
        logger.warning("Environment restore warning: %s", e)


def process_task_v4(task: Task, engine: AgentEngine) -> None:
    """V4 任务处理：使用 AgentEngine 替代旧版 process_task"""
    global _current_task_id
    if is_task_cancelled(task.task_id):
        logger.info("Skip cancelled task %s", task.task_id)
        return

    _current_task_id = task.task_id
    set_active_task(task.task_id)
    CURRENT_TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_TASK_FILE.write_text(task.task_id, encoding="utf-8")

    ws = create_workspace(task.task_id)

    memory = get_memory_service()
    if memory.enabled:
        if not memory.health_ok():
            logger.warning(
                "TencentDB memory enabled but gateway unhealthy at %s — recall/capture skipped",
                memory.gateway_url,
            )
        else:
            recall = memory.recall_for_task(task.task_id, task.raw_requirement)
            write_memory_recall_file(ws, recall)

    # 保存环境快照
    configs_path = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    original_configs = configs_path.read_text(encoding="utf-8") if configs_path.exists() else ""
    original_branch, _ = _snapshot_git_state()

    try:
        # 根据任务状态决定从哪里开始处理
        if task.resume_from_gate:
            logger.info("L2 gate approved, resuming from coding for %s", task.task_id)
            engine.resume_from_gate(task)
        elif task.resume_after_clarification:
            logger.info("User clarification received, resuming for %s", task.task_id)
            engine.resume_after_clarification(task)
        else:
            if task.current_state == State.PENDING.value:
                transition(task.task_id, State.PLANNING, "dequeue", task)
            engine.process_task(task)
    finally:
        if memory.enabled and memory.health_ok():
            memory.capture_task_turn(
                task.task_id,
                task.raw_requirement,
                f"[task finished] state={task.current_state}",
            )
            memory.task_session_end(task.task_id)
        _current_task_id = ""
        clear_active_task()
        CURRENT_TASK_FILE.write_text("", encoding="utf-8")
        _restore_environment(task, original_branch)


# ------------------------------------------------------------------
# 主循环
# ------------------------------------------------------------------


def run_loop() -> None:
    """V4 主执行循环"""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 1. 配置预校验
    if not validate_config():
        logger.error("Config validation failed, aborting startup")
        sys.exit(1)

    # 2. 初始化数据库
    init_db()
    logger.info("V4 Executor starting — DB at %s", DB_FILE)

    # 3. 构建引擎
    engine = build_engine()

    # 4. 获取锁
    lock_fd = acquire_lock()
    if lock_fd is None:
        logger.error("Another executor is already running, exiting")
        sys.exit(1)

    try:
        while not _shutdown_requested:
            tasks = get_executable_tasks(limit=1)
            if not tasks:
                time.sleep(2)
                continue

            task = tasks[0]
            if is_task_cancelled(task.task_id):
                logger.info("Skip cancelled pending task %s", task.task_id)
                time.sleep(0.5)
                continue

            logger.info("=" * 60)
            logger.info("PROCESS %s | %s | %s", task.task_id, task.level, task.raw_requirement[:60])
            logger.info("=" * 60)

            try:
                process_task_v4(task, engine)
            except AgentFatalError as e:
                logger.error("Fatal error processing %s: %s", task.task_id, e)
            except Exception as e:
                logger.exception("Unexpected error processing %s: %s", task.task_id, e)
                try:
                    transition(task.task_id, State.FAILED, f"unexpected: {e}", task)
                except Exception:
                    pass

            # 短暂停顿，避免 CPU 空转
            time.sleep(1)
    finally:
        os.close(lock_fd)
        logger.info("V4 Executor stopped")


if __name__ == "__main__":
    run_loop()
