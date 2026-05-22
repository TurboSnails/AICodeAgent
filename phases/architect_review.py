#!/usr/bin/env python3
"""
Architect Review 阶段处理器 — V4 新增
职责：
1. 在 CODEX_REVIEW 通过后，由架构师角色评估代码结构
2. 检查 SOLID 原则、设计模式适用性、技术债风险
3. 评估是否需要重构，给出重构方案
4. 可配置：默认关闭，L2 或高复杂度任务可开启

与 CODEX_REVIEW 的区别：
- CODEX：逻辑正确性、回归风险
- ARCHITECT：代码结构、设计质量、技术债
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int, cfg_str
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases._review_utils import (
    list_changed_files,
    parse_codex_verdict,
    read_changed_sources,
    workspace_context,
)
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class ArchitectReviewHandler(PhaseHandler):
    """
    Architect Review 阶段：代码结构与设计质量评估。

    输入状态：ARCHITECT_REVIEW
    输出状态：
      - RED_TEAM_REVIEW（架构评审通过）
      - CORRECTING（建议重构，生成重构 prompt）
      - FAILED（超出最大重试次数）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client
        self._max_retries = cfg_int("features.architect_max_retry", 1)

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 检查是否启用
        if not cfg_bool("features.architect_review_enabled", False):
            logger.info("Architect review disabled by config, skipping for %s", task.task_id)
            return PhaseResult(
                State.RED_TEAM_REVIEW,
                "architect_review skipped (disabled)",
            )

        # 2. 检查任务等级是否适用
        allowed_levels = cfg_str("features.architect_for_levels", "L2").split(",")
        allowed_levels = [lvl.strip().upper() for lvl in allowed_levels if lvl.strip()]
        if task.level.upper() not in allowed_levels:
            logger.info(
                "Architect review skipped for level %s (only %s)",
                task.level,
                allowed_levels,
            )
            return PhaseResult(
                State.RED_TEAM_REVIEW,
                f"architect_review skipped (level {task.level} not in {allowed_levels})",
            )

        if self._ai is None:
            raise AgentRecoverableError("ArchitectReviewHandler missing ai_client")

        # 3. 获取变更文件和源码
        changed_files = list_changed_files(base_branch=task.base_branch or "")
        source_snapshot = read_changed_sources(changed_files, max_total=20000)

        if not changed_files:
            logger.info("No changed files for %s, skip architect review", task.task_id)
            return PhaseResult(State.RED_TEAM_REVIEW, "no changed files, skip architect_review")

        # 4. 构建 prompt
        prompt = self._build_architect_prompt(
            requirement=task.raw_requirement,
            changed_files=changed_files,
            source_snapshot=source_snapshot,
            workspace=workspace,
        )

        # 5. 调用 LLM
        try:
            output = self._ai.call(prompt, context=workspace_context(workspace))
        except Exception as e:
            logger.exception("Architect review LLM call failed: %s", e)
            raise AgentRecoverableError(f"Architect review LLM call failed: {e}")

        report = output or "（空输出）"
        (workspace / "architect_review.md").write_text(report, encoding="utf-8")

        # 6. 解析 verdict（兜底：若报告中存在 MUST 级别问题但 Verdict 误写 PASS，强制 FAIL）
        passed = parse_codex_verdict(report)
        if passed and re.search(r"^\s*-\s*MUST\b", report, re.MULTILINE | re.IGNORECASE):
            passed = False
            logger.warning(
                "Architect review for %s: MUST items found but Verdict is PASS, forcing FAIL",
                task.task_id,
            )

        architect_round = task.phase_counters.get("architect", 0)

        if passed:
            logger.info("Architect review PASS for %s", task.task_id)
            return PhaseResult(
                State.RED_TEAM_REVIEW,
                "architect review passed",
                {"architect_report": report},
            )

        # 7. 失败处理
        architect_round += 1
        logger.warning(
            "Architect review FAIL (round %d/%d)",
            architect_round,
            self._max_retries,
        )
        task.error_log = report[:4000]
        task.phase_counters["architect"] = architect_round
        save_task(task)

        if architect_round > self._max_retries:
            raise AgentRecoverableError(
                f"architect review max retries exceeded ({self._max_retries})"
            )

        fix_prompt = self._build_architect_fix_prompt(
            task.raw_requirement, report, architect_round + 1
        )
        (workspace / "fix_prompt.md").write_text(fix_prompt, encoding="utf-8")

        return PhaseResult(
            State.CORRECTING,
            f"architect fail round {architect_round}",
            {
                "architect_report": report,
                "fix_prompt": fix_prompt,
            },
        )

    @staticmethod
    def _build_architect_prompt(
        requirement: str,
        changed_files: list[str],
        source_snapshot: str,
        workspace: Path,
    ) -> str:
        consensus = ""
        cp = workspace / "consensus.md"
        if cp.exists():
            consensus = cp.read_text(encoding="utf-8")[:6000]

        codex_report = ""
        cr = workspace / "codex_review.md"
        if cr.exists():
            codex_report = cr.read_text(encoding="utf-8")[:2000]

        return f"""
你是 **架构师 Agent** — 负责评估代码结构质量，而非逻辑正确性。

## 原始需求
{requirement}

## 共识方案（节选）
{consensus}

## 已通过的 Codex 逻辑审查摘要
{codex_report}

## 本次变更文件
{"".join(f"- `{f}`\n" for f in changed_files[:30])}

## 变更源码（节选）
```
{source_snapshot[:15000]}
```

## 审查维度（专注结构与质量）
1. **SOLID 原则**：
   - 单一职责：类/函数是否承担了过多职责？
   - 开闭原则：新增功能是否依赖修改旧代码？
   - 依赖倒置：是否直接依赖具体实现而非抽象？

2. **设计模式**：
   - 当前场景是否有更适合的设计模式？
   - 是否过度设计？（避免为模式而模式）

3. **代码异味**：
   - 函数过长（>50行）、类过大（>300行）
   - 嵌套过深（>4层）
   - 魔法数字、重复代码、上帝类
   - 参数过多（>5个）

4. **可维护性**：
   - 命名是否表意清晰
   - 是否有足够的内聚性
   - 模块间耦合是否过高

5. **技术债风险**：
   - 是否引入了需要后续清理的临时方案
   - 是否有 TODO/FIXME 未处理

## 输出格式
```markdown
## Verdict
PASS 或 FAIL

## Architecture Issues
- （无则写 无）

## Design Pattern Suggestions
- （当前适用或应替换的模式建议）

## Refactor Priority
- MUST（阻塞性，必须修复）
- SHOULD（重要，建议修复）
- COULD（可选，有余力再处理）

## Suggested Refactor
- （FAIL 时给出具体重构方案，包括文件级别的改动建议）
```

**判定规则**：
- 存在 MUST 级别架构问题 → Verdict 必须为 **FAIL**
- 仅有 SHOULD/COULD 级别建议 → **PASS**（但需在报告中标注）
- 代码结构合理、无明显异味 → **PASS**
"""

    @staticmethod
    def _build_architect_fix_prompt(
        requirement: str, review_report: str, attempt: int
    ) -> str:
        return f"""
架构师评审未通过（第 {attempt} 次重构）

原始需求: {requirement}

架构师评审报告:
{review_report}

重构规则:
1. 优先修复 MUST 级别问题
2. 遵循报告中建议的设计模式
3. 保持与 consensus 一致的设计约定
4. 不要引入新的第三方依赖
5. 不要运行任何 Gradle 命令
6. 使用 === FILE: path === 格式输出完整文件内容
"""
