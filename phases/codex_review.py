#!/usr/bin/env python3
"""
Codex Review 阶段处理器 — V4 重构
职责：
1. 使用 AIClient + _review_utils 进行逻辑/回归审查
2. PASS -> RED_TEAM_REVIEW
3. FAIL -> CORRECTING（生成 codex fix prompt）
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases._review_utils import (
    build_codex_review_prompt,
    list_changed_files,
    parse_and_save_fix_plan,
    parse_codex_verdict,
    workspace_context,
)
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class CodexReviewHandler(PhaseHandler):
    """
    Codex Review 阶段：逻辑审查与回归检测。

    输入状态：CODEX_REVIEW
    输出状态：
      - RED_TEAM_REVIEW（Codex 通过）
      - CORRECTING（Codex 失败，尝试修正）
      - FAILED（超出最大重试次数）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client
        self._max_retries = cfg_int("retries.codex_review", 2)

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        codex_round = task.phase_counters.get("codex", 0)

        if self._ai is None:
            raise AgentRecoverableError("CodexReviewHandler missing ai_client")

        # 1. 获取变更文件
        changed_files = list_changed_files(base_branch=task.base_branch or "")

        # 2. 构建上下文和 prompt
        context = workspace_context(workspace)
        prompt = build_codex_review_prompt(
            requirement=task.raw_requirement,
            workspace=workspace,
            changed_files=changed_files,
            impact_summary="",  # 未来可接入 graph_bridge 影响面分析
        )

        # 3. 调用 LLM（优先 Codex CLI，回退 claude --print）
        try:
            output = self._ai.call_codex(prompt, context=context)
        except Exception as e:
            logger.exception("Codex review LLM call failed: %s", e)
            raise AgentRecoverableError(f"Codex review LLM call failed: {e}")

        report = output or "（空输出）"
        (workspace / "codex_review.md").write_text(report, encoding="utf-8")

        # 尝试解析结构化 Fix Plan
        fix_plan = parse_and_save_fix_plan(report, workspace)

        passed = parse_codex_verdict(report)

        if passed:
            logger.info("Codex review PASS for %s", task.task_id)
            # 若 architect_review 被全局关闭，直接跳过到 red_team_review
            if not cfg_bool("features.architect_review_enabled", False):
                return PhaseResult(
                    State.RED_TEAM_REVIEW,
                    "codex review passed, architect_review disabled → red_team_review",
                    {"codex_report": report},
                )
            return PhaseResult(
                State.ARCHITECT_REVIEW,
                "codex review passed → architect_review",
                {"codex_report": report},
            )

        # 失败处理
        codex_round += 1
        logger.warning("Codex review FAIL (round %d/%d)", codex_round, self._max_retries)
        task.error_log = report[:4000]
        task.phase_counters["codex"] = codex_round
        task.phase_counters["last_fail_stage"] = "codex_review"
        save_task(task)

        if codex_round > self._max_retries:
            raise AgentRecoverableError(
                f"codex review max retries exceeded ({self._max_retries})"
            )

        fix_prompt = self._build_codex_fix_prompt(task.raw_requirement, report, codex_round + 1)
        (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

        return PhaseResult(
            State.CORRECTING,
            f"codex fail round {codex_round}",
            {
                "codex_report": report,
                "fix_prompt": fix_prompt,
                "fix_plan_path": str(workspace / "codex_fix_plan.json") if fix_plan else None,
            },
        )

    @staticmethod
    def _build_codex_fix_prompt(requirement: str, review_report: str, attempt: int) -> str:
        return f"""
Codex 逻辑审查未通过（第 {attempt} 次修正）

原始需求: {requirement}

审查报告:
{review_report}

修正规则:
1. 仅修改审查报告中指出的问题
2. 不要修改与问题无关的文件
3. 不要引入新的第三方依赖
4. 不要运行任何 Gradle 命令
5. 使用 === FILE: path === 格式输出完整文件内容
"""
