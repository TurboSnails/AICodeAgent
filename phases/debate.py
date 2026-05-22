#!/usr/bin/env python3
"""
Debate 阶段处理器 — V4 重构
职责：
1. 构建 Architect / Figma / Guardian 三方 prompt
2. 并行调用三个 Agent（ThreadPoolExecutor）
3. 支持重试与降级为 Architect Only
4. 输出三方结果到 workspace
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_int
from utils.logging_config import get_logger
from engine.state_machine import State, Task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT


class DebateHandler(PhaseHandler):
    """
    Debate 阶段：Multi-Agent 并行辩论。

    输入状态：DEBATING
    输出状态：
      - CONSENSUS（三方输出完整）
      - CORRECTING（输出不完整，尝试修正）
      - FAILED（降级也失败）
    """

    def __init__(self, ai_client=None):
        self._ai = ai_client

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        max_retries = cfg_int("retries.debate", 1)

        for attempt in range(max_retries + 1):
            logger.info("Debate attempt %d/%d for %s", attempt + 1, max_retries + 1, task.task_id)
            if self._try_debate(task, workspace):
                return PhaseResult(State.CONSENSUS, "debate complete")
            if attempt < max_retries:
                logger.info("Debate retry in 5s...")
                time.sleep(5)

        # 重试耗尽，降级为 Architect Only
        logger.warning("All debate retries exhausted, falling back to Architect Only")
        if self._run_architect_only_fallback(task, workspace):
            return PhaseResult(State.CONSENSUS, "debate fallback to architect only")

        raise AgentRecoverableError("Debate stage failed or timed out")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _try_debate(self, task: Task, workspace: Path) -> bool:
        """单次 Debate 调用"""
        self._build_prompts(task, workspace)
        self._build_context_file(workspace)

        agents = [
            ("architect", workspace / "architect_proposal.md", "architect_proposal_output.md"),
            ("figma", workspace / "figma_audit.md", "figma_audit_output.md"),
            ("guardian", workspace / "guardian_review.md", "guardian_review_output.md"),
        ]
        context_file = workspace / "claude_context.md"
        deadline = time.time() + cfg_int("timeouts.debate", 600)
        results: dict[str, bool] = {}

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = []
            for agent_name, prompt_file, output_name in agents:
                remaining = max(30, int(deadline - time.time()))
                per_agent_timeout = min(cfg_int("timeouts.agent_single", 500), remaining)
                futures.append(
                    pool.submit(
                        self._run_single_agent,
                        agent_name, prompt_file, output_name, context_file, workspace, per_agent_timeout,
                    )
                )
            try:
                for fut in as_completed(futures, timeout=cfg_int("timeouts.debate", 600)):
                    if time.time() > deadline:
                        break
                    name, ok = fut.result()
                    results[name] = ok
            except Exception as e:
                logger.error("Debate timeout or error: %s", e)
                return False

        if len(results) < 3 or not all(results.values()):
            logger.warning("Incomplete or empty debate outputs: %s", results)
            return False
        return True

    def _run_single_agent(
        self,
        agent_name: str,
        prompt_file: Path,
        output_name: str,
        context_file: Path,
        workspace: Path,
        timeout: int,
    ) -> tuple[str, bool]:
        """调用单个 Agent"""
        prompt = prompt_file.read_text(encoding="utf-8")
        logger.info("Calling Agent %s (timeout=%ds)...", agent_name, timeout)

        if self._ai is None:
            logger.error("AI client not available")
            return agent_name, False

        # 使用 context_file 内容作为上下文
        context = ""
        if context_file.exists():
            context = context_file.read_text(encoding="utf-8")

        output = self._ai.call(prompt, context=context, timeout=timeout)
        out_path = workspace / output_name
        out_path.write_text(output, encoding="utf-8")
        ok = bool(output.strip())
        logger.info("Agent %s done (%d chars, ok=%s)", agent_name, len(output), ok)
        return agent_name, ok

    def _run_architect_only_fallback(self, task: Task, workspace: Path) -> bool:
        """Debate 失败后降级为仅运行 Architect"""
        logger.info("Running Architect Only for %s", task.task_id)
        self._build_prompts(task, workspace)
        context_file = workspace / "claude_context.md"
        prompt_file = workspace / "architect_proposal.md"
        output_file = "architect_proposal_output.md"

        context = context_file.read_text(encoding="utf-8") if context_file.exists() else ""
        prompt = prompt_file.read_text(encoding="utf-8")
        output = self._ai.call(prompt, context=context, timeout=cfg_int("timeouts.agent_single", 500))

        if not output.strip():
            logger.error("Architect Only also failed")
            return False

        out_path = workspace / output_file
        out_path.write_text(output, encoding="utf-8")

        fallback_consensus = f"""# consensus.md — 降级方案（Debate 失败，Architect Only）

## 任务概述
- 需求: {task.raw_requirement}
- 等级: {task.level}
- 降级原因: Multi-Agent Debate 超时或失败，回退至 Architect 单 Agent

## 技术方案 (Architect Only)
{output[:4000]}

## 合规审查 (降级跳过)
- ⚠️ Guardian 未参与：请在 Review 时特别关注 SiteRules 和 Compose 规范

## 视觉规范 (降级跳过)
- ⚠️ Figma Auditor 未参与：请人工确认视觉资产需求

## 最终文件清单
（请从 Architect 方案中手动提取文件路径填入此处表格）

## 视觉资产映射
（降级模式下未执行自动资产去重，请人工确认）
"""
        (workspace / "consensus.md").write_text(fallback_consensus, encoding="utf-8")
        logger.info("consensus.md generated from Architect Only (%d chars)", len(fallback_consensus))
        return True

    def _build_prompts(self, task: Task, workspace: Path) -> None:
        """构造三方 Agent prompt 并写入 workspace"""
        requirement = task.raw_requirement
        site_hint = task.site_hint or "current"
        structure = self._get_project_structure_summary()

        # Agent A: Architect
        architect_prompt = f"""
你是一位 Android 资深架构师。写页面之前，**必须先读本地已有的类似页面和接口**，再结合需求出技术方案。

需求: {requirement}
目标站点: {site_hint}

{structure}

## 第一步：扫描本地同类页面（强制）
在给出方案前，请先分析本地已有的同类 Screen/Page：
1. 找出与需求最相似的 `*Screen*.kt` / `*Page*.kt` 文件
2. 分析这些页面的 UI 模式：用了哪些 Compose 组件、图片如何加载
3. 分析 State 设计：有哪些 UIState 字段、如何管理 Loading / Error 状态
4. 提取接口层代码（UseCase / Repository / Api）

## 第二步：制定技术方案
基于以上代码分析，输出：
1. 需要修改/新增的文件清单
2. 每个文件的核心改动描述
3. 数据流设计（ViewModel → UseCase → Repository）
4. 状态管理方案（MVI / MVVM）
5. 图片加载策略决策
6. 任何需要注意的接口签名或依赖约束

规则：
- 不要向人类提问，基于现有代码做出最专业假设
- 使用 TextUtils.equals() 进行 site enName 比较
- UIState 必须为不可变 data class
"""
        (workspace / "architect_proposal.md").write_text(architect_prompt, encoding="utf-8")

        # Agent B: Figma Auditor
        asset_analysis = ""
        analysis_file = workspace / "asset_analysis.json"
        if analysis_file.exists():
            asset_analysis = analysis_file.read_text(encoding="utf-8")

        figma_prompt = f"""
你是一位 Figma 视觉审计师。你的核心职责是：**先读本地代码，再看 Figma，最后决定下载什么图片**。

需求: {requirement}

## 第一步：阅读本地代码分析结果
```json
{asset_analysis}
```

请根据以上分析回答：
1. 同类页面中图片是怎么加载的？
2. 接口数据模型中是否已包含图片 URL 字段？
3. 本地已有的 drawable 中，是否有可以复用的图标/图片？

## 第二步：基于代码缺口精准定位 Figma 资产
不是遍历 Figma 全部节点，而是根据第一步的代码分析结果，**只关注当前页面实际引用的组件/图标**。

## 第三步：输出资产决策表
| Figma 节点名 | 本地状态 | 决策 | 理由 |

规则：
- **严禁盲目下载**：每个 Figma 资产都必须有明确的“下载理由”
- 资产命名使用 snake_case，前缀 ic_ 表示图标
"""
        (workspace / "figma_audit.md").write_text(figma_prompt, encoding="utf-8")

        # Agent C: Guardian
        guardian_prompt = f"""
你是一位项目安全/规范官，负责审查技术方案是否符合项目约束。

需求: {requirement}

请从以下维度审查，输出通过/不通过及原因：
1. 是否遵守 Compose 规范（collectAsStateWithLifecycle、ImmutableList 等）
2. 是否正确使用 SiteCapsRegistry 进行站点判断
3. 是否可能引入新的第三方依赖
4. UIState 是否为不可变 data class
5. 是否可能修改与需求无关的文件
6. 字符串资源是否放在正确的 siteRes 目录

规则：
- 安全约束优先级最高
- 给出具体修改建议，不要只说"不通过"
"""
        (workspace / "guardian_review.md").write_text(guardian_prompt, encoding="utf-8")

    def _build_context_file(self, workspace: Path) -> None:
        """构建辩论阶段上下文文件"""
        parts = []
        analysis = workspace / "asset_analysis.json"
        if analysis.exists():
            parts.append(f"\n## Local Code-First Asset Analysis\n```json\n{analysis.read_text(encoding='utf-8')}\n```\n")
        colors = workspace / "figma" / "colors.json"
        if colors.exists():
            parts.append(f"\n## Figma Colors\n```json\n{colors.read_text(encoding='utf-8')}\n```\n")
        (workspace / "claude_context.md").write_text("\n".join(parts), encoding="utf-8")

    @staticmethod
    def _get_project_structure_summary() -> str:
        """生成项目结构摘要"""
        lines = ["## Project Structure Summary"]
        for subdir in ["app/src/main/java/com/sport", "buildSrc/src/main/kotlin/site"]:
            p = PROJECT_ROOT / subdir
            if p.exists():
                files = sorted(p.rglob("*.kt"))[:30]
                lines.append(f"\n### {subdir}")
                for f in files:
                    rel = f.relative_to(PROJECT_ROOT)
                    lines.append(f"- {rel}")
        return "\n".join(lines)
