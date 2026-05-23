#!/usr/bin/env python3
"""
Requirement Review 阶段处理器 — V4 重构
职责：
1. 使用 AIClient + _review_utils 进行需求符合度审查
2. PASS -> GIT_COMMITTING
3. FAIL -> CORRECTING（生成 fix prompt）
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases._review_utils import (
    build_requirement_acceptance_prompt,
    list_changed_files,
    parse_and_save_fix_plan,
    parse_codex_verdict,
    workspace_context,
)
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class RequirementReviewHandler(PhaseHandler):
    """
    Requirement Review 阶段：需求符合度审查。

    输入状态：REQUIREMENT_REVIEW
    输出状态：
      - GIT_COMMITTING（全部通过）
      - CORRECTING（审查失败，尝试修正）
      - FAILED（超出最大重试次数）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client
        self._max_retries = cfg_int("retries.acceptance_review", 2)

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        acceptance_round = task.phase_counters.get("acceptance", 0)

        if self._ai is None:
            raise AgentRecoverableError("RequirementReviewHandler missing ai_client")

        # 1. 获取变更文件和上下文
        changed_files = list_changed_files(base_branch=task.base_branch or "")
        context = workspace_context(workspace)

        # 2. 读取上一轮 Codex 报告（若存在）
        prior_codex_report = ""
        codex_report_path = workspace / "codex_review.md"
        if codex_report_path.exists():
            prior_codex_report = codex_report_path.read_text(encoding="utf-8")

        prompt = build_requirement_acceptance_prompt(
            requirement=task.raw_requirement,
            workspace=workspace,
            changed_files=changed_files,
            prior_codex_report=prior_codex_report,
        )

        # 3. 调用 LLM
        try:
            output = self._ai.call_codex(prompt, context=context)
        except Exception as e:
            logger.exception("Requirement review LLM call failed: %s", e)
            raise AgentRecoverableError(f"Requirement review LLM call failed: {e}")

        report = output or "（空输出）"
        (workspace / "requirement_review.md").write_text(report, encoding="utf-8")

        # 尝试解析结构化 Fix Plan
        fix_plan = parse_and_save_fix_plan(report, workspace)

        passed = parse_codex_verdict(report)

        if passed:
            logger.info("Requirement review PASS for %s", task.task_id)
            return PhaseResult(
                State.GIT_COMMITTING,
                "requirement review passed",
                {"requirement_report": report},
            )

        # 失败处理
        acceptance_round += 1
        logger.warning(
            "Requirement review FAIL (round %d/%d)",
            acceptance_round,
            self._max_retries,
        )
        task.error_log = report[:4000]
        task.phase_counters["acceptance"] = acceptance_round
        task.phase_counters["last_fail_stage"] = "requirement_review"
        save_task(task)

        if acceptance_round > self._max_retries:
            raise AgentRecoverableError(
                f"requirement review max retries exceeded ({self._max_retries})"
            )

        fix_prompt = self._build_fix_prompt(
            task.raw_requirement, report, acceptance_round + 1
        )
        (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

        return PhaseResult(
            State.CORRECTING,
            f"acceptance fail round {acceptance_round}",
            {
                "requirement_report": report,
                "fix_prompt": fix_prompt,
                "fix_plan_path": str(workspace / "requirement_fix_plan.json") if fix_plan else None,
            },
        )

    @staticmethod
    def _build_fix_prompt(requirement: str, review_report: str, attempt: int) -> str:
        return f"""
需求验收审查未通过（第 {attempt} 次修正）

原始需求: {requirement}

审查报告:
{review_report}

修正规则:
1. 确保原始需求中的每个功能点都被正确实现
2. 检查验收标准是否全部满足
3. 修复明显的逻辑错误和性能问题
4. 不要修改与问题无关的文件
5. 不要引入新的第三方依赖
6. 使用 === FILE: path === 格式输出完整文件内容
"""
