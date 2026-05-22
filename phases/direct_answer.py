#!/usr/bin/env python3
"""
Direct Answer 阶段处理器 — V4 路由新增
职责：
1. 处理 EXPLAIN 类型请求
2. 直接调用 AI 生成回答
3. 保存为 answer.md
4. 流转到 COMPLETED
"""

from __future__ import annotations

from pathlib import Path

from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult
from utils.config_loader import cfg_bool, cfg_int
from utils.logging_config import get_logger
from utils.memory_context import load_memory_recall_from_workspace, prepend_memory_to_parts
from services.tencent_memory_service import get_memory_service

logger = get_logger(__name__)


class DirectAnswerHandler(PhaseHandler):
    """
    EXPLAIN 请求处理器。
    直接调用 AI 生成回答，保存为 answer.md，流转到 COMPLETED。

    输入状态：DIRECT_ANSWER
    输出状态：
      - COMPLETED（回答生成完毕）
      - FAILED（AI 调用失败）
    """

    def __init__(self, ai_client=None, notification_service=None):
        self._ai = ai_client
        self._notify = notification_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        logger.info("DirectAnswer for %s | %s", task.task_id, task.raw_requirement[:60])

        if self._ai is None:
            logger.error("AI client not available for direct_answer")
            return PhaseResult(State.FAILED, "AI client not available")

        # 构建上下文
        context = self._build_context(task, workspace)
        prompt = self._build_prompt(task)

        try:
            timeout = cfg_int("timeouts.agent_single", 500)
            answer = self._ai.call(prompt, context=context, timeout=timeout)

            if not answer or not answer.strip():
                logger.warning("Direct answer returned empty for %s", task.task_id)
                return PhaseResult(State.FAILED, "direct answer returned empty")

            # 保存回答
            answer_path = workspace / "answer.md"
            answer_path.write_text(answer, encoding="utf-8")
            logger.info("Saved answer.md for %s (%d chars)", task.task_id, len(answer))

            get_memory_service().capture_task_turn(
                task.task_id, task.raw_requirement, answer
            )

            # 通知用户
            if self._notify:
                self._notify.notify_task_completed(task, artifact_path=answer_path)

            return PhaseResult(State.COMPLETED, "direct answer generated")

        except Exception as e:
            logger.exception("Direct answer failed for %s: %s", task.task_id, e)
            return PhaseResult(State.FAILED, f"direct answer failed: {e}")

    def _build_context(self, task: Task, workspace: Path) -> str:
        """构建回答所需的上下文。"""
        parts = []

        from utils.project_guides import append_project_guides_to_parts

        append_project_guides_to_parts(parts, max_chars_per_file=4000)

        # RAG 上下文（如有）
        rag = workspace / "coding_context.md"
        if rag.exists():
            parts.append(f"\n## RAG Context\n{rag.read_text(encoding='utf-8')[:3000]}")

        prepend_memory_to_parts(parts, load_memory_recall_from_workspace(workspace))

        return "\n".join(parts)

    def _build_prompt(self, task: Task) -> str:
        return f"""
你是一位资深 Android 开发专家。用户提出了一个关于代码或项目的问题，请给出清晰、准确的回答。

## 用户问题
{task.raw_requirement}

## 回答要求
1. 先给出核心结论（一句话总结）
2. 然后展开详细解释
3. 如涉及代码，给出关键代码片段并标注文件路径
4. 如涉及架构决策，说明原因和权衡
5. 使用中文回答（代码保留英文）
6. 如果问题涉及特定站点（site_hint={task.site_hint or 'unspecified'}），请结合该站点的上下文

请直接输出回答内容，不需要输出元数据或总结。
"""
