#!/usr/bin/env python3
"""
Self Review 阶段处理器 — V4 新增
职责：
1. BUILDING 通过后，让 AI Agent 自查代码变更
2. 检查命名规范、明显逻辑漏洞、是否违背 consensus 约定
3. 输出 confidence_score（0-10）和问题列表
4. 高分直接流转到 CODEX_REVIEW，低分流转到 CORRECTING

设计原则：轻量级、低成本、高覆盖。一次快速 LLM 调用过滤 30-50% 低级错误。
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.exceptions import AgentFatalError, AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases._review_utils import (
    list_task_relevant_changed_files,
    parse_codex_verdict,
    workspace_context,
)
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class SelfReviewHandler(PhaseHandler):
    """
    Self Review 阶段：构建通过后的快速自查。

    输入状态：SELF_REVIEW
    输出状态：
      - CODEX_REVIEW（自查通过，自信度 >= threshold）
      - CORRECTING（自查发现需修复的问题）
      - FAILED（LLM 调用失败且无法恢复）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client
        self._confidence_threshold = cfg_int("features.self_review_threshold", 7)
        self._max_retries = cfg_int("features.self_review_max_retry", 1)

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 检查是否被配置禁用
        if not cfg_bool("features.self_review_enabled", True):
            logger.info("Self review disabled by config, skipping for %s", task.task_id)
            return PhaseResult(
                State.CODEX_REVIEW,
                "self_review skipped (disabled)",
            )

        # L0 编译验收：跳过自审查（避免把 AICodeAgent 分支脏文件误判为需求变更）
        if task.level == "L0" and not cfg_bool("features.self_review_for_l0", False):
            logger.info("L0 task %s — skip self_review (compile-only path)", task.task_id)
            return PhaseResult(
                State.CODEX_REVIEW,
                "L0 compile-only: self_review skipped",
            )

        if self._ai is None:
            raise AgentRecoverableError("SelfReviewHandler missing ai_client")

        # 2. 获取变更文件
        changed_files = list_task_relevant_changed_files(
            task.raw_requirement,
            base_branch=task.base_branch or "",
        )
        if not changed_files:
            logger.info("No changed files for %s, skip self review", task.task_id)
            return PhaseResult(State.CODEX_REVIEW, "no changed files, skip self_review")

        # 3. 构建 prompt
        prompt = self._build_self_review_prompt(
            requirement=task.raw_requirement,
            changed_files=changed_files,
            workspace=workspace,
        )

        # 4. 调用 LLM（轻量级调用，不需要 codex）
        try:
            output = self._ai.call(prompt, context=workspace_context(workspace))
        except Exception as e:
            logger.exception("Self review LLM call failed: %s", e)
            # 自评失败不阻断主流程，降级到 CODEX_REVIEW
            return PhaseResult(
                State.CODEX_REVIEW,
                f"self_review LLM failed: {e}, fallback to codex",
            )

        report = output or "（空输出）"
        (workspace / "self_review.md").write_text(report, encoding="utf-8")

        # 5. 解析自信度和 verdict
        confidence = self._parse_confidence(report)
        passed = parse_codex_verdict(report)

        # 兜底：Verdict 缺失但 confidence 足够且无严重问题时视为 PASS
        if not passed and confidence >= self._confidence_threshold:
            has_critical = bool(
                re.search(
                    r"(MUST|严重|critical|blocker)", report, re.IGNORECASE
                )
            )
            if not has_critical:
                passed = True
                logger.info(
                    "Self review for %s: Verdict missing but confidence=%d "
                    "and no critical issues, treating as PASS",
                    task.task_id,
                    confidence,
                )

        logger.info(
            "Self review for %s: confidence=%d/10, verdict=%s",
            task.task_id,
            confidence,
            "PASS" if passed else "FAIL",
        )

        # 6. 决策：自信度足够且 verdict PASS → CODEX_REVIEW
        if passed and confidence >= self._confidence_threshold:
            return PhaseResult(
                State.CODEX_REVIEW,
                f"self_review passed (confidence {confidence}/10)",
                {"self_review_report": report},
            )

        # 7. 不自查通过 → 更新计数器并检查重试上限
        self_review_round = task.phase_counters.get("self_review", 0) + 1
        task.phase_counters["self_review"] = self_review_round
        task.phase_counters["last_fail_stage"] = "self_review"
        task.error_log = report[:4000]
        save_task(task)

        logger.warning(
            "Self review FAIL (round %d/%d)",
            self_review_round,
            self._max_retries,
        )

        if self_review_round > self._max_retries:
            raise AgentFatalError(
                f"self review max retries exceeded ({self._max_retries})"
            )

        fix_prompt = self._build_self_review_fix_prompt(
            task.raw_requirement, report, confidence
        )
        (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

        return PhaseResult(
            State.CORRECTING,
            f"self_review failed (confidence {confidence}/10, round {self_review_round}/{self._max_retries})",
            {
                "self_review_report": report,
                "fix_prompt": fix_prompt,
            },
        )

    @staticmethod
    def _build_self_review_prompt(
        requirement: str,
        changed_files: list[str],
        workspace: Path,
    ) -> str:
        consensus = ""
        cp = workspace / "consensus.md"
        if cp.exists():
            consensus = cp.read_text(encoding="utf-8")[:4000]

        return f"""
你是 **Self Review Agent** — 刚完成编码的开发者，现在需要快速自查本次变更。

## 原始需求
{requirement}

## 共识方案（节选）
{consensus}

## 本次变更文件
{"".join(f"- `{f}`\n" for f in changed_files[:30])}

## 自查维度（快速检查，不需要深度分析）
1. **命名规范**：函数/变量名是否清晰、是否符合项目惯例（驼峰/下划线等）。
2. **逻辑一致性**：代码是否满足 consensus 中的设计约定；是否有明显遗漏的分支。
3. **边界条件**：空值、越界、非法输入是否有处理。
4. **回归风险**：是否误改了无关文件；是否有硬编码站点名而未用 SiteCapsRegistry。
5. **代码异味**：过长函数（>50行）、嵌套过深（>4层）、魔法数字、重复代码块。

## 输出格式
```markdown
## Confidence Score
0-10 的整数（10 = 完全自信，0 = 严重问题必须修复）

## Verdict
PASS 或 FAIL

## Quick Issues
- （发现的任何问题，无则写 无）

## Suggested Fixes
- （FAIL 时给出最简修复建议）
```

**规则**：存在明显逻辑漏洞、命名混乱、或违背 consensus → Verdict 必须为 **FAIL**。
仅轻微风格问题不影响行为 → **PASS**，但 Confidence 可以低于 10。
"""

    @staticmethod
    def _parse_confidence(output: str) -> int:
        """从 LLM 输出中解析 Confidence Score（0-10）"""
        if not output:
            return 0
        m = re.search(
            r"##\s*Confidence Score\s*\n\s*(\d+)", output, re.IGNORECASE
        )
        if m:
            return max(0, min(10, int(m.group(1))))
        # 回退：如果 Verdict 是 PASS 但没给分数，默认 7
        if parse_codex_verdict(output):
            return 7
        return 0

    @staticmethod
    def _build_self_review_fix_prompt(
        requirement: str, review_report: str, confidence: int
    ) -> str:
        return f"""
自审查未通过（自信度 {confidence}/10），需要修正。

原始需求: {requirement}

自审查报告:
{review_report}

修正规则:
1. 仅修复报告中指出的问题
2. 不要修改与问题无关的文件
3. 不要引入新的第三方依赖
4. 不要运行任何 Gradle 命令
5. 使用 === FILE: path === 格式输出完整文件内容
"""
