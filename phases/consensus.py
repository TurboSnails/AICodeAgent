#!/usr/bin/env python3
"""
Consensus 阶段处理器 — V4 重构
职责：
1. 读取三方 Agent 输出
2. 调用 Consensus Agent 生成 consensus.md
3. 验证 consensus（Guardian 约束检查）
4. 复杂度逃逸检测（L0/L1 触及核心文件 → 强制升级 L2）
5. L2 任务流转到 WAITING_GATE
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task, transition
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)

# Guardian 约束正则模式
GUARDIAN_CONSTRAINT_PATTERNS = [
    (r"禁止.*新依赖|禁止.*第三方|不允许.*新依赖|不允许.*第三方|No new dependency|No third-party", "禁止新依赖"),
    (r"必须使用\s+SiteCapsRegistry|SiteCapsRegistry", "必须使用 SiteCapsRegistry"),
    (r"UIState.*不可变|immutable data class|UIState.*data class", "UIState 不可变"),
    (r"collectAsStateWithLifecycle|必须使用.*collectAsStateWithLifecycle", "collectAsStateWithLifecycle"),
    (r"TextUtils\.equals|必须使用.*TextUtils", "TextUtils.equals"),
    (r"ImmutableList|kotlinx\.collections\.immutable", "ImmutableList"),
    (r"禁止空\s*try-catch|空\s*try-catch", "禁止空 try-catch"),
]


class ConsensusHandler(PhaseHandler):
    """
    Consensus 阶段：生成共识文档并做逃逸检测。

    输入状态：CONSENSUS
    输出状态：
      - WAITING_GATE（L2 需要人工核准）
      - CODING（L0/L1 通过）
      - CORRECTING（共识生成失败）
      - FAILED（无法恢复）
    """

    def __init__(self, ai_client=None, notification_service=None):
        self._ai = ai_client
        self._notify = notification_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 生成共识
        if not self._generate_consensus(task, workspace):
            raise AgentRecoverableError("Consensus generation failed")

        # 2. 验证共识（Guardian 约束检查）
        valid, violations, summary = self._validate_consensus(workspace)
        if not valid:
            logger.warning("Consensus validation failed: %s", summary)
            # 将验证结果写入 workspace 供修正阶段使用
            (workspace / "consensus_violations.json").write_text(
                json.dumps({"violations": violations, "summary": summary}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            raise AgentRecoverableError(f"Consensus validation failed: {summary}")

        # 3. 复杂度逃逸检测（L0/L1 -> L2）
        if task.level in ("L0", "L1"):
            should_esc, reasons = self._should_escalate_to_l2(workspace)
            if should_esc:
                logger.warning("[ESCAPE] Complexity escalation to L2: %s", "; ".join(reasons))
                task.level = "L2"
                save_task(task)
                self._record_escape(workspace, "COMPLEXITY_ESCALATION", "\n".join(reasons))

        # 4. L2 进入人工核准门控
        if task.level == "L2":
            task.gate_deadline = (datetime.now() + timedelta(hours=24)).isoformat()
            save_task(task)
            if self._notify:
                self._notify.notify_l2_gate(task)
            return PhaseResult(State.WAITING_GATE, "L2 gate waiting for /continue")

        # 5. [新增] REVIEW_ONLY 请求直接进入 CodexReview
        if getattr(task, "request_type", "code") == "review_only":
            logger.info("REVIEW_ONLY task %s — skip architect_planning, go to codex_review", task.task_id)
            return PhaseResult(State.CODEX_REVIEW, "review_only — consensus passed, skip to review")

        # 6. L0/L1 进入架构规划阶段
        return PhaseResult(State.ARCHITECT_PLANNING, "consensus passed")

    # ------------------------------------------------------------------
    # 共识生成
    # ------------------------------------------------------------------

    def _generate_consensus(self, task: Task, workspace: Path) -> bool:
        """调用 Consensus Agent 生成 consensus.md"""
        architect = self._read_output(workspace, "architect_proposal_output.md")
        figma = self._read_output(workspace, "figma_audit_output.md")
        guardian = self._read_output(workspace, "guardian_review_output.md")

        # 降级模式（Architect Only）时，figma/guardian 可能为空
        if not architect:
            logger.error("No architect output for consensus")
            return False

        prompt = self._build_consensus_prompt(task, architect, figma, guardian)
        context = (workspace / "claude_context.md").read_text(encoding="utf-8") if (workspace / "claude_context.md").exists() else ""

        if self._ai is None:
            logger.error("AI client not available")
            return False

        max_retry = cfg_int("retries.consensus", 2)
        for attempt in range(max_retry + 1):
            try:
                output = self._ai.call(prompt, context=context, timeout=cfg_int("timeouts.agent_single", 500))
                if output.strip():
                    (workspace / "consensus.md").write_text(output, encoding="utf-8")
                    logger.info("consensus.md generated (%d chars)", len(output))
                    return True
            except Exception as e:
                logger.warning("Consensus attempt %d failed: %s", attempt + 1, e)

        return False

    @staticmethod
    def _build_consensus_prompt(task: Task, architect: str, figma: str, guardian: str) -> str:
        return f"""
你是一位 Consensus Agent（共识仲裁者）。三方 Agent 已就以下需求给出各自观点，请你综合出最终方案。

需求: {task.raw_requirement}

---
## Agent A (Architect) 技术方案
{architect[:3000]}

---
## Agent B (Figma Auditor) 视觉规范
{figma[:2000]}

---
## Agent C (Guardian) 合规审查
{guardian[:2000]}

---
请输出 consensus.md，包含：
1. 任务概述（需求 + 判定等级）
2. 技术方案摘要（Architect）
3. 视觉规范摘要（Figma Auditor）
4. 合规审查结果（Guardian）
5. 最终文件清单（表格：文件路径、操作、说明）
6. 视觉资产映射（表格：Figma 节点名、本地复用/新增、决策原因）

规则：
- Guardian 的安全约束优先级最高，必须被满足
- 如果 Architect 和 Guardian 冲突，以 Guardian 为准，但尽量保留 Architect 的架构意图
- 不要输出解释性文字，只输出共识方案的 Markdown
"""

    @staticmethod
    def _read_output(workspace: Path, filename: str) -> str:
        path = workspace / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    # ------------------------------------------------------------------
    # 共识验证
    # ------------------------------------------------------------------

    def _validate_consensus(self, workspace: Path) -> tuple[bool, list[dict], str]:
        """验证共识是否满足 Guardian 约束"""
        consensus_path = workspace / "consensus.md"
        if not consensus_path.exists():
            return False, [], "consensus.md not found"

        consensus_text = consensus_path.read_text(encoding="utf-8")

        # 提取 Guardian 约束
        guardian_path = workspace / "guardian_review_output.md"
        if not guardian_path.exists():
            # 降级模式跳过 Guardian 验证
            return True, [], "guardian output missing, skip validation"

        guardian_text = guardian_path.read_text(encoding="utf-8")
        constraints = self._extract_guardian_constraints(guardian_text)
        violations = self._check_architect_against_constraints(consensus_text, constraints)

        if violations:
            summary = "; ".join(f"{v['label']}: {v['detail']}" for v in violations)
            return False, violations, summary

        return True, [], "validation passed"

    @staticmethod
    def _extract_guardian_constraints(guardian_text: str) -> list[dict]:
        constraints = []
        for pattern, label in GUARDIAN_CONSTRAINT_PATTERNS:
            if re.search(pattern, guardian_text, re.IGNORECASE):
                constraints.append({"label": label, "pattern": pattern})
        return constraints

    @staticmethod
    def _check_architect_against_constraints(architect_text: str, constraints: list[dict]) -> list[dict]:
        violations = []
        text_lower = architect_text.lower()
        for c in constraints:
            label = c["label"]
            if label == "禁止新依赖":
                if "implementation(" in text_lower or "api(" in text_lower:
                    violations.append({"label": label, "detail": "detected new dependency suggestion"})
            elif label == "必须使用 SiteCapsRegistry":
                if "sitecapsregistry" not in text_lower:
                    violations.append({"label": label, "detail": "SiteCapsRegistry not mentioned"})
            elif label == "UIState 不可变":
                if "var " in text_lower and "uistate" in text_lower:
                    violations.append({"label": label, "detail": "mutable var found in UIState context"})
            elif label == "collectAsStateWithLifecycle":
                if "collectasstatewithlifecycle" not in text_lower and "collectasstate" in text_lower:
                    violations.append({"label": label, "detail": "using collectAsState instead of collectAsStateWithLifecycle"})
        return violations

    # ------------------------------------------------------------------
    # 逃逸检测
    # ------------------------------------------------------------------

    @staticmethod
    def _should_escalate_to_l2(workspace: Path) -> tuple[bool, list[str]]:
        """检测复杂度是否应强制升级 L2"""
        consensus_path = workspace / "consensus.md"
        if not consensus_path.exists():
            return False, []

        text = consensus_path.read_text(encoding="utf-8").lower()
        reasons: list[str] = []

        # 文件数量过多
        file_count = text.count(".kt") + text.count(".xml")
        if file_count > 8:
            reasons.append(f"too many files ({file_count})")

        # 触及核心文件
        core_patterns = ["buildsrc", "theme", "registry", "database", "migration", "navigation"]
        for p in core_patterns:
            if p in text:
                reasons.append(f"touches core component: {p}")

        # 跨站点修改
        if "allsite" in text or "全站" in text:
            reasons.append("cross-site modification")

        return bool(reasons), reasons

    @staticmethod
    def _record_escape(workspace: Path, escape_type: str, reason: str) -> None:
        """记录逃逸事件"""
        from datetime import datetime
        escape_log = workspace / "escape_history.log"
        timestamp = datetime.now().isoformat()
        with escape_log.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {escape_type}: {reason}\n")
        logger.info("Escape recorded: %s | %s", escape_type, reason)
