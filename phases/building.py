#!/usr/bin/env python3
"""
Building 阶段处理器 — V4 重构
职责：
1. 调用 BuildService 执行 Gradle 构建
2. 解析错误日志
3. 成功则流转到 CODEX_REVIEW
4. 失败则流转到 CORRECTING（携带错误摘要）
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError, BuildFailureError
from utils.config_loader import cfg_bool
from utils.logging_config import get_logger
from utils.project_guides import read_build_policy
from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class BuildingHandler(PhaseHandler):
    """
    Building 阶段：Gradle 构建与测试。

    输入状态：BUILDING
    输出状态：
      - CODEX_REVIEW（构建通过）
      - CORRECTING（构建失败，生成 fix prompt）
    """

    def __init__(self, build_service=None):
        self._build = build_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        if self._build is None:
            raise AgentRecoverableError("BuildService not available")

        try:
            success, log = self._build.build(
                task.task_id,
                workspace,
                level=task.level,
                requirement=task.raw_requirement,
            )
        except BuildFailureError as e:
            # 构建失败，解析错误并生成修正提示
            errors = self._build.parse_errors(str(e))
            task.error_log = errors
            save_task(task)

            # 生成 fix prompt 供 correcting 阶段使用
            fix_prompt = self._build_fix_prompt(task.raw_requirement, errors, task.attempt_count + 1)
            (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

            logger.warning("Build failed for %s: %s", task.task_id, errors[:200])
            return PhaseResult(
                State.CORRECTING,
                f"build failed: {errors[:200]}",
                {"errors": errors, "fix_prompt": fix_prompt},
            )

        if success:
            logger.info("Build passed for %s", task.task_id)
            policy = read_build_policy(workspace)
            if task.level == "L0" and (
                (policy and policy.assemble_only)
                or not cfg_bool("features.self_review_for_l0", False)
            ):
                return PhaseResult(
                    State.GIT_COMMITTING,
                    "L0 compile-only build passed, skip review pipeline",
                    {"build_log": log},
                )

            # 断点续传：若上一次失败在某个 review 阶段，直接从那里重跑，跳过更早的阶段
            _RESUME_STATES = {
                "codex_review": State.CODEX_REVIEW,
                "architect_review": State.ARCHITECT_REVIEW,
                "red_team_review": State.RED_TEAM_REVIEW,
                "requirement_review": State.REQUIREMENT_REVIEW,
            }
            last_fail = task.phase_counters.get("last_fail_stage", "")
            if last_fail in _RESUME_STATES:
                resume_state = _RESUME_STATES[last_fail]
                logger.info("Build passed, resuming from %s (skipping earlier review stages)", last_fail)
                task.phase_counters.pop("last_fail_stage", None)
                save_task(task)
                return PhaseResult(
                    resume_state,
                    f"build passed, resume from {last_fail}",
                    {"build_log": log},
                )

            return PhaseResult(
                State.SELF_REVIEW,
                "build passed",
                {"build_log": log},
            )

        # 理论上不会到达这里（BuildFailureError 已捕获）
        raise AgentRecoverableError("Build returned unexpected failure")

    @staticmethod
    def _build_fix_prompt(requirement: str, errors: str, attempt: int) -> str:
        return f"""
构建/测试失败（第 {attempt} 次重试）

原始需求: {requirement}

Gradle 错误摘要:
{errors}

修复规则:
1. 仅修改与当前需求直接相关的文件
2. 如果是测试中的 Context 问题，使用 Robolectric RuntimeEnvironment.getApplication()
3. 如果是资源缺失，在 strings.xml 或对应 siteRes 下补充
4. 如果是 import 错误，检查包名和依赖
5. 不要引入新的第三方依赖
6. 不要运行任何 Gradle 命令
"""

    def on_exit(self, task: Task, workspace: Path, result: PhaseResult) -> None:
        """构建完成后保存日志路径"""
        log = result.artifacts.get("build_log", "")
        if log:
            log_path = workspace / "build.log"
            log_path.write_text(log, encoding="utf-8")
