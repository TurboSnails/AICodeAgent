#!/usr/bin/env python3
"""
Coding 阶段处理器 — V4 重构
职责：
1. 构建编码上下文（consensus.md + asset_map + 项目规范）
2. 调用 AI Client 生成代码
3. 安全应用代码变更（GitService.apply_code_changes）
4. 站点切换与 clean（如果需要）
5. 空输出 / 失败处理
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT


class CodingHandler(PhaseHandler):
    """
    Coding 阶段：调用 Claude 生成代码并安全应用。

    输入状态：CODING
    输出状态：
      - BUILDING（代码已应用，进入构建）
      - CORRECTING（空输出或应用失败，尝试修正）
      - FAILED（超出最大重试次数）

    注意：此 handler 不处理 while 循环重试逻辑，重试由 Engine 驱动
    （Engine 从 CORRECTING 状态可再次流转到 CODING）。
    """

    def __init__(
        self,
        ai_client=None,
        git_service=None,
    ):
        self._ai = ai_client
        self._git = git_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 确保分支已创建（首次编码时）
        if not task.branch:
            if self._git is None:
                raise AgentRecoverableError("GitService not available for branch creation")
            base = task.base_branch or self._git.get_current_branch()
            task.base_branch = base
            task.branch = self._git.create_agent_branch(task.task_id, base)
            save_task(task)
            logger.info("Created agent branch %s for %s", task.branch, task.task_id)

        # 2. 构建编码上下文
        context_file = self._build_context_file(task, workspace)

        # 3. 构造编码 prompt
        prompt = self._build_coding_prompt(task, workspace)

        # 4. 调用 Claude 编码
        if self._ai is None:
            raise AgentRecoverableError("AI client not available")

        claude_output = self._ai.call(
            prompt,
            context=context_file.read_text(encoding="utf-8") if context_file.exists() else "",
            timeout=cfg_int("timeouts.claude_code", 1800),
        )

        # 5. 空输出检测
        if not claude_output.strip():
            logger.warning("Claude returned empty output for %s", task.task_id)
            raise AgentRecoverableError("Claude returned empty output")

        # 6. 安全应用代码变更
        if self._git is None:
            raise AgentRecoverableError("GitService not available for applying changes")

        applied, blocked = self._git.apply_code_changes(claude_output)
        logger.info("Applied %d files, blocked %d for %s", len(applied), len(blocked), task.task_id)

        if blocked:
            # 记录安全拦截但不阻塞流程（blocked 文件已在安全日志中记录）
            import json
            (workspace / "blocked_files.json").write_text(
                json.dumps([{"path": p, "reason": r} for p, r in blocked]),
                encoding="utf-8",
            )

        if not applied:
            raise AgentRecoverableError("No files were applied from Claude output")

        # 7. 保存本次输出供审计
        (workspace / "last_claude_output.md").write_text(claude_output, encoding="utf-8")

        return PhaseResult(
            State.BUILDING,
            f"coding done: {len(applied)} files applied",
            {"applied_files": applied, "blocked_files": blocked},
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_context_file(self, task: Task, workspace: Path) -> Path:
        """构建编码阶段上下文文件"""
        parts = []

        # 项目规范（如有 ARCHITECTURE_v4.md 则注入）
        arch_md = PROJECT_ROOT / "ARCHITECTURE_v4.md"
        if arch_md.exists():
            parts.append(arch_md.read_text(encoding="utf-8"))

        # 共识方案
        consensus = workspace / "consensus.md"
        if consensus.exists():
            parts.append(f"\n## Consensus\n{consensus.read_text(encoding='utf-8')}")

        # 资产映射
        asset_map = workspace / "asset_map.json"
        if asset_map.exists():
            parts.append(f"\n## Asset Map\n```json\n{asset_map.read_text(encoding='utf-8')}\n```")

        # 修正提示（如果存在）
        fix_prompt = workspace / "fix_prompt.md"
        if fix_prompt.exists():
            parts.append(f"\n## Fix Instructions\n{fix_prompt.read_text(encoding='utf-8')}")

        context_path = workspace / "coding_context.md"
        context_path.write_text("\n".join(parts), encoding="utf-8")
        return context_path

    @staticmethod
    def _build_coding_prompt(task: Task, workspace: Path) -> str:
        """构造编码阶段的主 prompt"""
        prompt = f"""
需求: {task.raw_requirement}

要求:
1. 严格遵循 workspace 内 consensus.md 与 asset_map.json
2. 不要运行任何 Gradle 命令或 git 命令
3. 不要修改与需求无关的文件
4. 不要添加新的第三方依赖
5. 使用 === FILE: path === 格式输出完整文件内容
6. 使用 TextUtils.equals() 进行 site enName 比较
7. UIState 必须为不可变 data class

请直接输出代码。
"""
        return prompt

    def on_exit(self, task: Task, workspace: Path, result: PhaseResult) -> None:
        """编码完成后记录文件清单"""
        applied = result.artifacts.get("applied_files", [])
        if applied:
            (workspace / "applied_files.log").write_text(
                "\n".join(applied), encoding="utf-8"
            )
