#!/usr/bin/env python3
"""
Architect Planning 阶段处理器 — V4 优化新增
职责：
1. 基于 consensus.md 进行结构化需求拆解
2. 结合本地代码做不确定性检测
3. 输出 plan.md + design.md + uncertainty_check.json
4. 存在 blocking uncertainty → WAITING_CLARIFICATION
5. 无 blocking uncertainty → CODING
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine.exceptions import AgentRecoverableError
from engine.state_machine import State, Task, save_task, transition
from phases.base import PhaseHandler, PhaseResult
from utils.config_loader import cfg_bool, cfg_int
from utils.logging_config import get_logger
from utils.paths import PROJECT_ROOT

logger = get_logger(__name__)


class ArchitectPlanningHandler(PhaseHandler):
    """
    Architect Planning 阶段：需求拆解 + 代码不确定性检测。

    输入状态：ARCHITECT_PLANNING
    输出状态：
      - CODING（拆解完成，无 blocking uncertainty）
      - WAITING_CLARIFICATION（存在 blocking uncertainty）
      - CORRECTING（AI 调用失败）
    """

    def __init__(self, ai_client=None, notification_service=None):
        self._ai = ai_client
        self._notify = notification_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        # 1. 检查配置是否跳过此阶段
        if cfg_bool("features.architect_planning_enabled", True) is False:
            logger.info("Architect planning disabled by config for %s", task.task_id)
            return PhaseResult(State.CODING, "architect_planning skipped by config")

        # L0 默认跳过（可通过配置覆盖）
        if task.level == "L0" and not cfg_bool("features.architect_planning_for_l0", False):
            logger.info("L0 task skips architect_planning for %s", task.task_id)
            return PhaseResult(State.CODING, "L0 fast path: skip architect_planning")

        # 2. 构建上下文
        context = self._build_context(task, workspace)

        # 3. 调用 AI 进行需求拆解 + 不确定性检测
        if self._ai is None:
            raise AgentRecoverableError("AI client not available for architect_planning")

        prompt = self._build_prompt(task)
        try:
            output = self._ai.call(prompt, context=context, timeout=cfg_int("timeouts.agent_single", 300))
        except Exception as e:
            logger.exception("Architect planning AI call failed: %s", e)
            raise AgentRecoverableError(f"architect_planning AI call failed: {e}")

        if not output.strip():
            raise AgentRecoverableError("architect_planning returned empty output")

        # 4. 解析输出并保存产物
        self._save_artifacts(output, workspace)

        # 5. 检查不确定性
        uncertainties = self._parse_uncertainties(workspace)
        blocking = [u for u in uncertainties if u.get("severity", "").lower() == "blocking"]

        if blocking:
            logger.warning("Found %d blocking uncertainties for %s", len(blocking), task.task_id)
            return self._enter_code_clarification(task, workspace, blocking)

        logger.info("Architect planning passed for %s, proceeding to coding", task.task_id)
        return PhaseResult(State.CODING, "architect_planning complete, no blocking uncertainties")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_context(self, task: Task, workspace: Path) -> str:
        """构建 Architect Planning 所需的上下文。"""
        parts = []

        # 项目规范
        agents_md = PROJECT_ROOT / "AGENTS.md"
        if agents_md.exists():
            parts.append(agents_md.read_text(encoding="utf-8")[:6000])

        # Consensus
        consensus = workspace / "consensus.md"
        if consensus.exists():
            parts.append(f"\n## Consensus\n{consensus.read_text(encoding='utf-8')[:8000]}")

        # RAG 最佳实践（如有）
        rag = workspace / "coding_context.md"
        if rag.exists():
            parts.append(f"\n## RAG Context\n{rag.read_text(encoding='utf-8')[:4000]}")

        # 已有代码上下文（consensus 中提到的文件）
        code_context = self._extract_code_context(workspace)
        if code_context:
            parts.append(f"\n## Related Code\n{code_context}")

        return "\n".join(parts)

    def _build_prompt(self, task: Task) -> str:
        return f"""
你是一位资深 Android 架构师。Consensus 已产出最终方案，但在编码前，请结合以下代码上下文，进行最后的需求拆解和不确定性检测。

## 原始需求
{task.raw_requirement}

## 任务信息
- 等级: {task.level}
- 站点: {task.site_hint or 'unspecified'}

## 输出要求
请严格按以下结构输出（使用 === FILE: path === 格式）：

=== FILE: plan.md ===
# 实现计划

## 1. 实现步骤（按顺序，每步一个小标题）
### Step 1: ...
- 目标：...
- 修改文件：...
- 关键逻辑：...

### Step 2: ...
...

## 2. 文件清单
| 文件路径 | 操作（新增/修改/删除） | 说明 |

## 3. 接口变更清单
| 接口/方法 | 变更类型 | 说明 |

=== FILE: design.md ===
# 设计决策

## 1. 架构决策
- 使用 MVVM / MVI / 其他？
- State 管理方案
- 数据流设计

## 2. 状态机设计
- UIState 字段定义
- Event / Effect 定义

## 3. 依赖关系
- 新增依赖？（禁止）
- 现有依赖复用

=== FILE: uncertainty_check.json ===
{{
  "uncertainties": [
    {{
      "question": "具体问题描述",
      "severity": "blocking | warning",
      "reason": "为什么这是不确定的",
      "suggested_approach": "建议的处理方式"
    }}
  ],
  "summary": "不确定性总结"
}}

## 规则
1. **blocking**：如果在代码中找不到需求的某个关键点（如接口签名、数据模型字段、缺失的依赖），必须标记为 blocking
2. **warning**：如果存在模棱两可的设计选择（如用 Button 还是 IconButton），标记为 warning
3. **无 uncertainties 时输出空数组**：{{"uncertainties": [], "summary": "无不确定性"}}
4. 不要向人类提问，先输出完整分析
"""

    def _save_artifacts(self, output: str, workspace: Path) -> None:
        """从 AI 输出中提取并保存三个产物文件。"""
        files = self._extract_file_blocks(output)

        for filename, content in files.items():
            path = workspace / filename
            path.write_text(content, encoding="utf-8")
            logger.info("Saved %s (%d chars)", filename, len(content))

        # 如果没有解析到，保存原始输出供调试
        if not files:
            (workspace / "architect_planning_raw.md").write_text(output, encoding="utf-8")
            logger.warning("Could not parse structured output, saved raw to architect_planning_raw.md")

    @staticmethod
    def _extract_file_blocks(text: str) -> dict[str, str]:
        """提取 === FILE: path === ... === END FILE === 格式的文件块。"""
        pattern = r"===\s*FILE:\s*(.+?)\s*===(.*?)==="
        matches = re.findall(pattern, text, re.DOTALL)
        result = {}
        for filename, content in matches:
            # 去掉可能的 END FILE 后缀
            fname = filename.strip()
            if fname.endswith("END FILE"):
                fname = fname.replace("END FILE", "").strip()
            result[fname] = content.strip()
        return result

    @staticmethod
    def _parse_uncertainties(workspace: Path) -> list[dict[str, Any]]:
        """解析 uncertainty_check.json 中的不确定性列表。"""
        path = workspace / "uncertainty_check.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("uncertainties", [])
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse uncertainty_check.json: %s", e)
            return []

    def _enter_code_clarification(
        self,
        task: Task,
        workspace: Path,
        uncertainties: list[dict[str, Any]],
    ) -> PhaseResult:
        """进入代码级澄清等待态。"""
        lines = [
            "# 代码澄清\n",
            "类型: code\n",
            "来源阶段: architect_planning\n",
            f"原因: 在需求拆解过程中发现 {len(uncertainties)} 个 blocking 级不确定性\n",
            "## 待用户回答\n",
        ]
        for i, u in enumerate(uncertainties, 1):
            lines.append(f"{i}. **{u.get('question', '未命名问题')}**\n")
            lines.append(f"   - 原因: {u.get('reason', '')}\n")
            lines.append(f"   - 建议方案: {u.get('suggested_approach', '')}\n")

        (workspace / "clarification_questions.md").write_text(
            "".join(lines), encoding="utf-8"
        )

        # 记录到任务历史中
        task.code_clarification_history.append({
            "stage": "architect_planning",
            "timestamp": datetime.now().isoformat(),
            "uncertainties": uncertainties,
        })
        task.clarification_type = "code"
        save_task(task)

        if self._notify:
            self._notify.notify_code_clarification(task, [u.get("question", "") for u in uncertainties])

        return PhaseResult(
            State.WAITING_CLARIFICATION,
            f"code clarification needed: {len(uncertainties)} blocking uncertainties",
            {"uncertainties": uncertainties, "clarification_type": "code"},
        )

    @staticmethod
    def _extract_code_context(workspace: Path) -> str:
        """从 consensus 中提取相关文件路径，读取部分代码作为上下文。"""
        consensus = workspace / "consensus.md"
        if not consensus.exists():
            return ""

        text = consensus.read_text(encoding="utf-8")
        # 提取 .kt / .xml 文件路径
        file_paths = re.findall(r"`?([\w/]+\.(?:kt|xml|gradle))`?", text)

        context_parts = []
        seen = set()
        for rel in file_paths[:10]:  # 最多 10 个文件
            if rel in seen:
                continue
            seen.add(rel)
            path = PROJECT_ROOT / rel
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8")
                    # 只取前 2000 字符作为上下文
                    context_parts.append(f"### {rel}\n```kotlin\n{content[:2000]}\n```\n")
                except OSError:
                    pass

        return "\n".join(context_parts)
