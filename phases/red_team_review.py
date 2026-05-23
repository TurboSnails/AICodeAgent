#!/usr/bin/env python3
"""
Red Team Review 阶段处理器 — V4 重构
职责：
1. 使用 AIClient + _review_utils 进行红队攻击视角审查
2. PASS -> REQUIREMENT_REVIEW
3. FAIL -> CORRECTING（生成 red_team fix prompt）
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int, cfg_str
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases._review_utils import (
    build_red_team_prompt,
    list_changed_files,
    parse_and_save_fix_plan,
    parse_codex_verdict,
    workspace_context,
)
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class RedTeamReviewHandler(PhaseHandler):
    """
    Red Team Review 阶段：红队攻击视角审查。

    输入状态：RED_TEAM_REVIEW
    输出状态：
      - REQUIREMENT_REVIEW（红队审查通过）
      - CORRECTING（红队审查失败，尝试修正）
      - FAILED（超出最大重试次数或该等级不启用红队）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 检查是否启用红队审查
        if not cfg_bool("features.red_team_enabled", True):
            logger.info("Red Team disabled by config, skipping for %s", task.task_id)
            return PhaseResult(State.REQUIREMENT_REVIEW, "red_team skipped (disabled)")

        # 2. 检查当前任务等级是否适用
        allowed_levels = cfg_str("features.red_team_for_levels", "L2").split(",")
        allowed_levels = [lvl.strip().upper() for lvl in allowed_levels if lvl.strip()]
        if task.level.upper() not in allowed_levels:
            logger.info(
                "Red Team skipped for level %s (only %s)", task.level, allowed_levels
            )
            return PhaseResult(
                State.REQUIREMENT_REVIEW,
                f"red_team skipped (level {task.level} not in {allowed_levels})",
            )

        if self._ai is None:
            raise AgentRecoverableError("RedTeamReviewHandler missing ai_client")

        red_team_round = task.phase_counters.get("red_team", 0)
        max_retries = cfg_int("features.red_team_max_retry", 1)

        # 3. 获取变更文件和上下文
        changed_files = list_changed_files(base_branch=task.base_branch or "")
        context = workspace_context(workspace)

        # 4. 读取上一轮 Codex 报告（若存在）
        prior_codex_report = ""
        codex_report_path = workspace / "codex_review.md"
        if codex_report_path.exists():
            prior_codex_report = codex_report_path.read_text(encoding="utf-8")

        prompt = build_red_team_prompt(
            requirement=task.raw_requirement,
            workspace=workspace,
            changed_files=changed_files,
            prior_codex_report=prior_codex_report,
        )

        # 5. 调用 LLM
        try:
            output = self._ai.call_codex(prompt, context=context)
        except Exception as e:
            logger.exception("Red Team review LLM call failed: %s", e)
            raise AgentRecoverableError(f"Red Team review LLM call failed: {e}")

        report = output or "（空输出）"
        (workspace / "red_team_audit.md").write_text(report, encoding="utf-8")

        # 尝试解析结构化 Fix Plan
        fix_plan = parse_and_save_fix_plan(report, workspace)

        passed = parse_codex_verdict(report)

        if passed:
            logger.info("Red Team review PASS for %s", task.task_id)
            return PhaseResult(
                State.REQUIREMENT_REVIEW,
                "red_team review passed",
                {"red_team_report": report},
            )

        # 失败处理
        red_team_round += 1
        logger.warning(
            "Red Team review FAIL (round %d/%d)", red_team_round, max_retries
        )
        task.error_log = report[:4000]
        task.phase_counters["red_team"] = red_team_round
        task.phase_counters["last_fail_stage"] = "red_team_review"
        save_task(task)

        if red_team_round > max_retries:
            raise AgentRecoverableError(
                f"red_team review max retries exceeded ({max_retries})"
            )

        fix_prompt = self._build_red_team_fix_prompt(
            task.raw_requirement, report, red_team_round + 1
        )
        (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

        return PhaseResult(
            State.CORRECTING,
            f"red_team fail round {red_team_round}",
            {
                "red_team_report": report,
                "fix_prompt": fix_prompt,
                "fix_plan_path": str(workspace / "red_team_fix_plan.json") if fix_plan else None,
            },
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _build_red_team_fix_prompt(self, requirement: str, report: str, attempt: int) -> str:
        """根据 Red Team 审查报告生成修正 prompt"""
        return f"""
## Red Team 审查失败（第 {attempt} 轮修正）

原始需求：
{requirement}

Red Team 发现的问题：
{report[:3000]}

请修复上述安全/边界/竞态/可维护性问题，确保代码通过 Red Team 审查。
不要改变原始需求的功能范围，只做防御性修复。
"""
