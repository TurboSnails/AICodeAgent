#!/usr/bin/env python3
"""
Correcting 阶段处理器 — V4 优化增强
职责：
1. 读取 fix_plan.md（结构化修复计划）或 fix_prompt.md（兼容旧路径）
2. 按优先级排序（Critical → High → Medium → Low）
3. 每轮只提取最高优先级的一批问题，生成 single_fix_prompt.md
4. 检测修复进度停滞，必要时将剩余问题转用户澄清（WAITING_CLARIFICATION）
5. 兼容旧路径：无 fix_plan 时仍使用 fix_prompt.md
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from engine.exceptions import AgentFatalError
from engine.state_machine import State, Task, save_task
from phases._fix_plan import FixItem, FixPlan, FixPriority
from phases.base import PhaseHandler, PhaseResult
from utils.config_loader import cfg_float, cfg_int, cfg_str
from utils.escape_detector import detect_unsolvable, record_escape
from utils.logging_config import get_logger

logger = get_logger(__name__)


class CorrectingHandler(PhaseHandler):
    """
    Correcting 阶段：修正前的优先级调度与逃逸检测。

    输入状态：CORRECTING
    输出状态：
      - CODING（按优先级单步修复）
      - WAITING_CLARIFICATION（多次失败/停滞，需用户决策）
      - FAILED（不可解检测触发 或 超出最大重试次数）
    """

    def __init__(self, notification_service=None):
        self._notify = notification_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        task.attempt_count += 1

        # 按来源阶段独立计数，避免不同类型修复共享全局上限
        fail_source = task.phase_counters.get("last_fail_stage", "build")
        source_key = f"correcting_{fail_source}"
        source_count = task.phase_counters.get(source_key, 0) + 1
        task.phase_counters[source_key] = source_count
        save_task(task)

        max_retries = cfg_int("retries.coding", 3) if task.max_retries is None else task.max_retries
        per_source_max = cfg_int(f"retries.correcting_{fail_source}", max_retries)

        # 1. 超出全局重试次数（安全网：所有来源合计）
        if task.attempt_count > max_retries * 2:
            logger.error(
                "Global max retries exceeded for %s (%d)",
                task.task_id,
                task.attempt_count,
            )
            raise AgentFatalError(f"global max retries exceeded ({task.attempt_count})")

        # 2. 超出该来源阶段的单独重试次数
        if source_count > per_source_max:
            logger.error(
                "Per-source max retries exceeded for %s: source=%s count=%d/%d",
                task.task_id,
                fail_source,
                source_count,
                per_source_max,
            )
            raise AgentFatalError(f"{fail_source} correcting max retries exceeded ({source_count}/{per_source_max})")

        # 2. 不可解检测（复用 escape_detector）
        error_history = self._collect_error_history(workspace)
        if len(error_history) >= 2:
            is_unsolvable, reason = detect_unsolvable(error_history)
            if is_unsolvable:
                logger.error("[ESCAPE] Unsolvable detected for %s: %s", task.task_id, reason)
                self._write_unrecoverable_error(workspace, task.raw_requirement, reason)
                record_escape(workspace, "UNSOLVABLE_LOOP", reason)
                raise AgentFatalError(f"unsolvable error loop: {reason}")

        # 3. 尝试加载结构化 FixPlan
        fix_plan = self._load_fix_plan(workspace)

        if fix_plan and fix_plan.items:
            return self._handle_fix_plan(task, workspace, fix_plan, max_retries)

        # 4. 无 FixPlan，回退到旧路径（fix_prompt.md）
        logger.info(
            "No fix_plan found, falling back to legacy fix_prompt for %s (attempt %d/%d)",
            task.task_id,
            task.attempt_count,
            max_retries,
        )
        return PhaseResult(
            State.CODING,
            f"legacy correcting attempt {task.attempt_count}",
            {"attempt_count": task.attempt_count},
        )

    # ------------------------------------------------------------------
    # FixPlan 驱动的优先级修复
    # ------------------------------------------------------------------

    def _handle_fix_plan(
        self,
        task: Task,
        workspace: Path,
        fix_plan: FixPlan,
        max_retries: int,
    ) -> PhaseResult:
        """基于 FixPlan 的优先级修复调度。"""
        sorted_items = fix_plan.sorted_items()

        # 判断是否应转用户澄清
        if self._should_escalate_to_user(task, sorted_items, workspace, max_retries):
            return self._enter_code_clarification(task, workspace, sorted_items)

        # 提取最高优先级的批次（同优先级最多 3 个）
        top_priority = sorted_items[0].priority
        batch = [item for item in sorted_items if item.priority == top_priority][:3]

        logger.info(
            "FixPlan batch for %s: priority=%s, items=%d (total critical=%d high=%d)",
            task.task_id,
            top_priority.value,
            len(batch),
            fix_plan.total_critical,
            fix_plan.total_high,
        )

        # 生成单步修复 prompt
        single_fix = self._build_single_fix_prompt(task, batch)
        (workspace / "single_fix_prompt.md").write_text(single_fix, encoding="utf-8")

        # 标记已提取的问题为 in_progress（追加到 fix_plan 的 metadata）
        self._mark_in_progress(workspace, batch)

        return PhaseResult(
            State.CODING,
            f"fix priority {top_priority.value}: {len(batch)} items",
            {
                "attempt_count": task.attempt_count,
                "fix_priority": top_priority.value,
                "fix_batch_size": len(batch),
                "fix_total_remaining": len(sorted_items),
            },
        )

    @staticmethod
    def _load_fix_plan(workspace: Path) -> FixPlan | None:
        """加载结构化修复计划。优先读 fix_plan.json（review 阶段写入），回退 fix_plan.md。"""
        for name in ("fix_plan.json", "fix_plan.md"):
            path = workspace / name
            if not path.exists():
                continue
            try:
                plan = FixPlan.read(path)
                if plan.items:
                    logger.debug("Loaded fix plan from %s (%d items)", name, len(plan.items))
                    return plan
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to parse %s: %s", name, e)
        return None

    def _should_escalate_to_user(
        self,
        task: Task,
        sorted_items: list[FixItem],
        workspace: Path,
        max_retries: int,
    ) -> bool:
        """
        判断是否应将修复问题转交给用户澄清。

        触发条件（全部满足）：
        1. attempt_count > max_retries * threshold_ratio（默认 0.6）
        2. 剩余问题中仍有 priority >= min_priority（默认 high）的问题
        3. 连续 stall_rounds 轮最高优先级问题未减少
        """
        threshold_ratio = cfg_float("correcting.user_clarify_threshold_ratio", 0.6)
        min_priority_str = cfg_str("correcting.user_clarify_min_priority", "high")
        stall_rounds = cfg_int("correcting.user_clarify_stall_rounds", 2)

        # 条件 1：尝试次数超过阈值
        if task.attempt_count <= int(max_retries * threshold_ratio):
            return False

        # 条件 2：仍有高优先级问题
        min_priority = FixPriority(min_priority_str)
        high_items = [i for i in sorted_items if FixPriority.order(i.priority) <= FixPriority.order(min_priority)]
        if not high_items:
            return False

        # 条件 3：连续多轮最高优先级问题未减少
        stall = self._detect_stall(workspace, stall_rounds)
        if not stall:
            return False

        logger.warning(
            "Escalating to user for %s: attempt=%d/%d, high_items=%d, stall=%d rounds",
            task.task_id,
            task.attempt_count,
            max_retries,
            len(high_items),
            stall_rounds,
        )
        return True

    @staticmethod
    def _detect_stall(workspace: Path, stall_rounds: int) -> bool:
        """检测修复进度是否停滞（连续 N 轮最高优先级问题未减少）。"""
        history_file = workspace / "fix_progress.json"
        if not history_file.exists():
            return False

        try:
            history = json.loads(history_file.read_text(encoding="utf-8"))
            rounds = history.get("rounds", [])
            if len(rounds) < stall_rounds:
                return False

            # 取最近 N 轮的最高优先级
            recent = rounds[-stall_rounds:]
            # 如果所有轮次的最高优先级相同，视为停滞
            priorities = [r.get("highest_priority") for r in recent]
            return len(set(priorities)) == 1 and priorities[0] is not None
        except (json.JSONDecodeError, ValueError):
            return False

    @staticmethod
    def _mark_in_progress(workspace: Path, batch: list[FixItem]) -> None:
        """记录修复进度，用于检测停滞。"""
        history_file = workspace / "fix_progress.json"
        history = {"rounds": []}
        if history_file.exists():
            try:
                history = json.loads(history_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass

        if batch:
            history["rounds"].append({
                "timestamp": datetime.now().isoformat(),
                "highest_priority": batch[0].priority.value,
                "batch_size": len(batch),
            })

        history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")

    def _enter_code_clarification(
        self,
        task: Task,
        workspace: Path,
        remaining_items: list[FixItem],
    ) -> PhaseResult:
        """将剩余高优先级问题转给用户澄清。"""
        # 只取 High 及以上的问题
        high_items = [i for i in remaining_items if FixPriority.order(i.priority) <= FixPriority.order(FixPriority.HIGH)]

        lines = [
            "# 代码澄清\n",
            "类型: code\n",
            "来源阶段: correcting\n",
            f"原因: 经过 {task.attempt_count} 轮自动修复后，仍有 {len(high_items)} 个高优先级问题无法解决\n",
            "## 剩余问题及建议\n",
        ]
        for i, item in enumerate(high_items, 1):
            lines.append(f"{i}. **[{item.priority.value.upper()}]** {item.category}\n")
            lines.append(f"   - 问题: {item.description}\n")
            if item.target_files:
                lines.append(f"   - 涉及文件: {', '.join(item.target_files)}\n")
            if item.suggested_fix:
                lines.append(f"   - AI 建议: {item.suggested_fix}\n")
            lines.append("\n")

        lines.append("## 请选择以下之一\n")
        lines.append("- 提供补充信息/决策，AI 将继续修复\n")
        lines.append("- 标记为 acceptable，AI 将忽略这些问题继续流程\n")
        lines.append("- 取消任务\n")

        (workspace / "clarification_questions.md").write_text(
            "".join(lines), encoding="utf-8"
        )

        # 记录到任务历史
        task.code_clarification_history.append({
            "stage": "correcting",
            "timestamp": datetime.now().isoformat(),
            "remaining_items": [i.to_dict() for i in high_items],
            "attempt_count": task.attempt_count,
        })
        task.clarification_type = "code"
        save_task(task)

        if self._notify:
            self._notify.notify_code_clarification(
                task,
                [f"[{i.priority.value}] {i.description[:60]}" for i in high_items],
            )

        return PhaseResult(
            State.WAITING_CLARIFICATION,
            f"code clarification after {task.attempt_count} failed attempts",
            {
                "remaining_items": [i.to_dict() for i in high_items],
                "attempt_count": task.attempt_count,
                "clarification_type": "code",
            },
        )

    @staticmethod
    def _build_single_fix_prompt(task: Task, batch: list[FixItem]) -> str:
        """为 Coding 阶段生成单步修复 prompt。"""
        lines = [
            f"## 修复任务（第 {task.attempt_count} 次修正）\n",
            f"原始需求: {task.raw_requirement}\n",
            f"\n本次需要修复 {len(batch)} 个问题（同优先级批次）：\n",
        ]
        for idx, item in enumerate(batch, 1):
            lines.append(f"\n### 问题 {idx}: [{item.priority.value.upper()}] {item.category}\n")
            lines.append(f"描述: {item.description}\n")
            if item.target_files:
                lines.append(f"目标文件: {', '.join(item.target_files)}\n")
            if item.suggested_fix:
                lines.append(f"建议修复: {item.suggested_fix}\n")

        lines.append("\n## 修复规则\n")
        lines.append("1. 仅修复上述指出的问题，不要修改无关文件\n")
        lines.append("2. 不要引入新的第三方依赖\n")
        lines.append("3. 不要运行任何 Gradle 命令\n")
        lines.append("4. 使用 === FILE: path === 格式输出完整文件内容\n")

        return "".join(lines)

    # ------------------------------------------------------------------
    # 兼容旧路径
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_error_history(workspace: Path) -> list[str]:
        """收集历史错误指纹（供 detect_unsolvable 使用）。"""
        history = []
        for filename in ["build.log", "codex_review.md", "requirement_review.md", "red_team_audit.md"]:
            path = workspace / filename
            if path.exists():
                text = path.read_text(encoding="utf-8")
                fingerprint = text[:500].strip()
                if fingerprint:
                    history.append(fingerprint)
        return history

    @staticmethod
    def _write_unrecoverable_error(workspace: Path, requirement: str, errors: str) -> None:
        """写入不可恢复错误记录。"""
        lines = [
            f"# Unrecoverable Error\n",
            f"## Time\n{datetime.now().isoformat()}\n",
            f"## Requirement\n{requirement}\n",
            f"## Errors\n{errors}\n",
        ]
        (workspace / "unrecoverable_error.md").write_text("".join(lines), encoding="utf-8")
