#!/usr/bin/env python3
"""
Planning 阶段处理器 — V4 重构
职责：
1. 自动生成需求文档 (requirement.md)
2. 准备资产上下文 (asset_analysis.json)
3. 请求类型分类与路由（新增）
4. 需求澄清判断：不明确则流转到 WAITING_CLARIFICATION
5. L0 快速路径判断
6. Auto level 判定
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int, cfg_str
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task, transition
from phases.base import PhaseHandler, PhaseResult
from services.request_classifier import RequestClassifier

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT

# Keywords that suggest visual asset involvement
FIGMA_KEYWORDS = [
    "figma", "设计稿", "ui", "界面", "颜色", "主题", "theme",
    "图标", "icon", "切图", "asset", "配色", "样式",
]

class PlanningHandler(PhaseHandler):
    """
    Planning 阶段：需求 intake + 资产准备 + 分类路由 + 澄清判断。

    输入状态：PLANNING
    输出状态：
      - WAITING_CLARIFICATION（需求不明确）
      - DEBATING（L1/L2 CODE_REQUEST 正常路径）
      - CODING（L0 快速路径 / CODE_REQUEST）
      - DIRECT_ANSWER（EXPLAIN_REQUEST）
      - ARCHITECT_PLANNING（DESIGN_ONLY）
      - CONSENSUS（REVIEW_ONLY）
    """

    def __init__(
        self,
        ai_client=None,
        notification_service=None,
    ):
        self._ai = ai_client
        self._notify = notification_service

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 自动判定 level（如果仍是 auto）
        if task.level == "auto":
            task.level = self._auto_level(task.raw_requirement)
            save_task(task)

        # 2. [新增] 请求类型分类
        if not task.request_type or task.request_type == "code":
            classifier = RequestClassifier()
            result = classifier.classify_with_fallback(task.raw_requirement, task.level)
            task.request_type = result.request_type
            save_task(task)
            logger.info(
                "Task %s classified as %s (confidence=%.2f)",
                task.task_id, task.request_type, result.confidence,
            )

        # 3. 生成需求文档（除非已澄清续跑且文件已存在）
        if not task.resume_after_clarification or not (workspace / "requirement.md").exists():
            self._generate_docs(task, workspace)

        # 4. 准备资产上下文（除非已澄清续跑且文件已存在）
        if not task.resume_after_clarification or not (workspace / "asset_analysis.json").exists():
            self._prepare_asset_context(task, workspace)

        # 5. 需求澄清判断（跳过条件：已澄清续跑 / 配置关闭 / L0 明确小改 / 非编码请求）
        if not task.resume_after_clarification and task.request_type == "code":
            needs_clarify, questions, reason = self._assess_clarity(task, workspace)
            if needs_clarify:
                return self._enter_clarification(task, workspace, questions, reason)

        # 6. [新增] 根据 request_type 路由到不同路径
        if task.request_type == "explain":
            return PhaseResult(State.DIRECT_ANSWER, "explain request — skip coding pipeline")

        if task.request_type == "design_only":
            return PhaseResult(State.ARCHITECT_PLANNING, "design request — skip debate/consensus")

        if task.request_type == "review_only":
            self._bootstrap_review_consensus(task, workspace)
            return PhaseResult(State.CODEX_REVIEW, "review_only — skip coding/building")

        # 7. 原有 CODE_REQUEST 路径：L0 快速路径跳过辩论
        if task.level == "L0":
            logger.info("L0 fast path — skip debate/consensus")
            self._bootstrap_l0_consensus(task, workspace)
            return PhaseResult(State.CODING, "L0 fast path")

        # 8. 正常路径 -> 辩论
        return PhaseResult(State.DEBATING, "planning complete")

    # ------------------------------------------------------------------
    # 子步骤实现
    # ------------------------------------------------------------------

    def _generate_docs(self, task: Task, workspace: Path) -> None:
        """生成 requirement.md"""
        has_figma = any(kw in task.raw_requirement.lower() for kw in FIGMA_KEYWORDS)
        lines = [
            f"# Requirement\n\n{task.raw_requirement}\n",
            f"\n## Meta\n- Level: {task.level}\n- Site: {task.site_hint or 'unspecified'}\n",
            f"- Figma involved: {'yes' if has_figma else 'no'}\n",
        ]
        (workspace / "requirement.md").write_text("".join(lines), encoding="utf-8")
        logger.info("Generated requirement.md for %s", task.task_id)

    def _prepare_asset_context(self, task: Task, workspace: Path) -> None:
        """准备资产分析上下文文件"""
        # V3 中调用 platform_figma / asset_manager 的简化版本
        # 实际逻辑保留在旧 orchestrator 中，此处只做文件占位和基础扫描
        analysis = {
            "task_id": task.task_id,
            "has_figma_hint": any(kw in task.raw_requirement.lower() for kw in FIGMA_KEYWORDS),
            "site_hint": task.site_hint,
            "scanned_at": datetime.now().isoformat(),
        }
        (workspace / "asset_analysis.json").write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Prepared asset context for %s", task.task_id)

    def _assess_clarity(self, task: Task, workspace: Path) -> tuple[bool, list[str], str]:
        """判断需求是否足够明确"""
        if cfg_bool("features.skip_clarification", False):
            return False, [], "skip by config"

        # 极简启发式判断（可替换为 LLM 调用）
        req = task.raw_requirement.lower()
        questions: list[str] = []

        # 缺少目标页面/模块
        if not re.search(r"(screen|page|fragment|activity|view|页面|界面|模块)", req):
            questions.append("目标页面/模块是什么？")

        # 缺少站点信息
        if not task.site_hint and not re.search(r"(site|站点|全站|所有站点)", req):
            questions.append("目标站点（enName）是什么，还是全站生效？")

        # 缺少验收标准
        if not re.search(r"(验收|测试|标准|验证|expect|should|must)", req):
            questions.append("验收标准或期望行为是什么？")

        # L0 明确小改豁免
        if task.level == "L0" and len(questions) <= 1:
            return False, [], "L0 clear enough"

        if not questions:
            return False, [], "requirement clear"

        return True, questions[:5], "missing key information"

    def _enter_clarification(
        self,
        task: Task,
        workspace: Path,
        questions: list[str],
        reason: str,
    ) -> PhaseResult:
        """进入需求澄清等待态"""
        lines = [f"# 需求澄清\n\n原因: {reason}\n", "## 待用户回答\n"]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q}\n")
        (workspace / "clarification_questions.md").write_text(
            "".join(lines), encoding="utf-8"
        )

        deadline = (datetime.now() + timedelta(hours=cfg_int("timeouts.clarification_hours", 48))).isoformat()
        task.clarification_deadline = deadline
        save_task(task)

        if self._notify:
            self._notify.notify_clarification(task, questions)

        return PhaseResult(
            State.WAITING_CLARIFICATION,
            f"needs clarification: {reason}",
            {"questions": questions},
        )

    def _bootstrap_l0_consensus(self, task: Task, workspace: Path) -> None:
        """L0 快速路径：生成最小化 consensus.md"""
        consensus = f"""# consensus.md — L0 快速路径

## 任务概述
- 需求: {task.raw_requirement}
- 等级: L0（跳过 Multi-Agent Debate）

## 技术方案
- L0 小改，直接由 Claude 编码

## 最终文件清单
（由编码阶段自动生成）

## 视觉资产映射
（L0 默认不处理 Figma 资产）
"""
        (workspace / "consensus.md").write_text(consensus, encoding="utf-8")
        logger.info("Bootstrapped L0 consensus for %s", task.task_id)

    def _bootstrap_review_consensus(self, task: Task, workspace: Path) -> None:
        """REVIEW_ONLY 请求：生成最小化共识，直接标记为 Review 上下文"""
        consensus = f"""# consensus.md — REVIEW_ONLY 模式

## 任务概述
- 需求: {task.raw_requirement}
- 类型: REVIEW_ONLY（用户请求代码审查，无需编码）

## Review 范围
- 用户提供的代码 / PR / 文件路径
- 由 CodexReview → ArchitectReview → RedTeamReview → RequirementReview 逐层审查
"""
        (workspace / "consensus.md").write_text(consensus, encoding="utf-8")
        logger.info("Bootstrapped REVIEW_ONLY consensus for %s", task.task_id)

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_level(requirement: str) -> str:
        """基于需求文本自动判定任务等级（与 V3 旧实现语义对齐）"""
        r = requirement.lower()
        # L2 指标
        if any(k in r for k in [
            "重构", "架构", "状态机", "跨模块", "核心", "网络层", "域名",
            "数据库迁移", "全局", "模块", "多站点", "全站", "theme",
            "设计系统", "database", "migration", "api 改版",
        ]):
            return "L2"
        # L0 指标
        if any(k in r for k in [
            "文案", "颜色", "间距", "字体大小", "string", "bug 修复",
            "修复崩溃", "typo", "strings.xml", "颜色值", "改一个字", "改个字",
        ]):
            return "L0"
        return "L1"
