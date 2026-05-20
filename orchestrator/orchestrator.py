#!/usr/bin/env python3
"""
Headless Orchestrator V3
- Multi-Agent Debate + Consensus
- Code-First asset_analysis 在辩论前注入
- L2 gate 核准后 resume_from_gate 续跑编码阶段
- Planning 后需求不明确 → waiting_clarification，用户 /reply 后再三方辩论
- Building 全绿 → codex_review（逻辑/回归）→ requirement_review（需求符合度/性能），FAIL → correcting
- Claude --print 只写代码；Orchestrator 跑 Gradle / git / PR
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional  # noqa: F401 — used in resolve_task_platform_site

from state_machine import Task, transition, State, save_task, get_task, approve_gate_resume
from asset_manager import process_visual_assets
from graph_bridge import build_architect_graph_context, semantic_search_files
from codex_review import run_codex_review, run_requirement_acceptance_review
from platform_figma import PlatformSiteResolved, resolve_site_hint, write_platform_site_meta

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "AICodeAgent" / "workspace"
CLAUDE_MD_PATH = PROJECT_ROOT / "AICodeAgent" / "orchestrator" / "CLAUDE_HEADLESS.md"
MAX_RETRIES = 3
BUILD_TIMEOUT = 15 * 60
CLAUDE_TIMEOUT = 30 * 60
DEBATE_TIMEOUT = int(os.environ.get("AGENT_DEBATE_TIMEOUT", "600"))
AGENT_TIMEOUT = int(os.environ.get("AGENT_SINGLE_TIMEOUT", "300"))
CONSENSUS_MAX_RETRY = int(os.environ.get("AGENT_CONSENSUS_MAX_RETRY", "2"))
CODEX_REVIEW_MAX_RETRY = int(os.environ.get("CODEX_REVIEW_MAX_RETRY", "2"))
ACCEPTANCE_REVIEW_MAX_RETRY = int(os.environ.get("ACCEPTANCE_REVIEW_MAX_RETRY", "2"))
CLARIFICATION_TIMEOUT_HOURS = int(os.environ.get("AGENT_CLARIFICATION_TIMEOUT_HOURS", "48"))
FIGMA_FETCH_SCRIPT = PROJECT_ROOT / "AICodeAgent" / "scripts" / "figma_fetch.sh"

FILE_MARKER = re.compile(r"===\s*FILE:\s*(.+?)\s*===")
END_MARKER = "=== END FILE ==="

FIGMA_KEYWORDS = [
    "figma", "设计稿", "ui", "界面", "颜色", "主题", "theme",
    "图标", "icon", "切图", "asset", "配色", "样式"
]

# ------------------------------------------------------------------
# V3 Multi-Agent Debate Engine
# ------------------------------------------------------------------

def get_project_structure_summary() -> str:
    """生成项目结构摘要，供 Agent A 参考"""
    lines = ["## Project Structure Summary"]
    # 扫描关键目录
    for subdir in ["app/src/main/java/com/sport", "buildSrc/src/main/kotlin/site"]:
        p = PROJECT_ROOT / subdir
        if p.exists():
            files = sorted(p.rglob("*.kt"))[:30]
            lines.append(f"\n### {subdir}")
            for f in files:
                rel = f.relative_to(PROJECT_ROOT)
                lines.append(f"- {rel}")
    return "\n".join(lines)


def build_debate_prompts(task: Task, workspace: Path):
    """为三个 Agent 构造辩论 prompt，写入 workspace"""
    requirement = task.raw_requirement
    site_hint = task.site_hint or "current"
    structure = get_project_structure_summary()

    # Agent A: Architect（代码优先：先读本地同类页面 + 接口，再出方案）
    architect_prompt = f"""
你是一位 Android 资深架构师。写页面之前，**必须先读本地已有的类似页面和接口**，再结合需求出技术方案。

需求: {requirement}
目标站点: {site_hint}

{structure}

## 第一步：扫描本地同类页面（强制）
在给出方案前，请先分析本地已有的同类 Screen/Page：
1. 找出与需求最相似的 `*Screen*.kt` / `*Page*.kt` 文件
2. 分析这些页面的 UI 模式：用了哪些 Compose 组件、图片如何加载（AsyncImage / painterResource / imageVector）
3. 分析 State 设计：有哪些 UIState 字段、如何管理 Loading / Error 状态
4. 提取接口层代码（UseCase / Repository / Api）：当前数据模型是否已包含图片字段、后端返回的是 URL 还是资源名

## 第二步：制定技术方案
基于以上代码分析，输出：
1. 需要修改/新增的文件清单
2. 每个文件的核心改动描述
3. 数据流设计（ViewModel → UseCase → Repository）
4. 状态管理方案（MVI / MVVM）
5. 图片加载策略决策：
   - 如果接口已有图片 URL 字段 → 使用 Coil AsyncImage 动态加载
   - 如果接口无图片字段但 UI 需要静态图 → 标记需要下载的 Figma 资产
   - 如果本地已有可复用图标 → 直接引用本地 drawable
6. 任何需要注意的接口签名或依赖约束

规则：
- 不要向人类提问，基于现有代码做出最专业假设
- 如果接口签名不明确，假设最常见的 Kotlin suspend 函数签名
- 使用 TextUtils.equals() 进行 site enName 比较
- UIState 必须为不可变 data class
"""
    graph_ctx = build_architect_graph_context(requirement, workspace)
    architect_prompt += f"\n\n{graph_ctx}"
    (workspace / "architect_proposal.md").write_text(architect_prompt, encoding="utf-8")

    # Agent B: Figma Auditor（代码优先的视觉审计）
    # 先尝试读取本地代码分析结果，供 Figma Auditor 做精准判断
    asset_analysis = ""
    analysis_file = workspace / "asset_analysis.json"
    if analysis_file.exists():
        asset_analysis = analysis_file.read_text(encoding="utf-8")

    figma_prompt = f"""
你是一位 Figma 视觉审计师。你的核心职责是：**先读本地代码，再看 Figma，最后决定下载什么图片**。

需求: {requirement}

---

## 第一步：阅读本地代码分析结果
以下是通过扫描本地同类页面和接口自动提取的分析（asset_analysis.json）：

```json
{asset_analysis}
```

请根据以上分析回答：
1. 同类页面中图片是怎么加载的？（Coil AsyncImage / painterResource / imageVector）
2. 接口数据模型中是否已包含图片 URL 字段？如果有，页面应优先使用接口 URL，而非下载静态图。
3. 本地已有的 drawable 中，是否有可以复用的图标/图片？

## 第二步：基于代码缺口精准定位 Figma 资产
不是遍历 Figma 全部节点，而是根据第一步的代码分析结果，**只关注当前页面实际引用的组件/图标**：

- 如果代码中用了 `Icon(imageVector = Icons.Default.Xxx)` → 检查 Figma 中是否有自定义图标需要替换
- 如果代码中用了 `AsyncImage(model = ...)` → 检查 Figma 中是否有占位图、缺省图、头像规范
- 如果代码中新增了一个按钮 → 检查 Figma 中该按钮的图标资源
- 如果接口没有图片字段但 Figma 有商品图 → 标记为"需同步 Architect 新增接口字段或本地 mock"

## 第三步：输出资产决策表

| Figma 节点名 | 本地状态 | 决策 | 理由 |
|-------------|---------|------|------|
| （示例）ic_delete | 本地已有 `ic_delete_forever.xml` | **复用本地** | 哈希相似度 97% |
| （示例）avatar_placeholder | 接口有 `avatarUrl` 字段 | **仅记录规范** | 运行时动态加载，无需静态图 |
| （示例）ic_new_feature | 本地无 | **下载** | 新功能图标，SVG → VectorDrawable |

同时输出：
- 颜色规范（如已知）
- 间距和布局建议（8dp 倍数）
- UI 组件清单（Dialog、Button、List 等）

规则：
- **严禁盲目下载**：每个 Figma 资产都必须有明确的“下载理由”，基于本地代码缺口
- 如果需求不含 UI 变更，明确说明"无需视觉资产"
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
- 安全约束优先级最高，任何可能破坏项目稳定性的方案必须驳回
- 给出具体修改建议，不要只说"不通过"
"""
    (workspace / "guardian_review.md").write_text(guardian_prompt, encoding="utf-8")


def _run_single_debate_agent(
    agent_name: str, prompt_file: Path, output_name: str, context_file: Path, workspace: Path, timeout: int
) -> tuple[str, bool]:
    prompt = prompt_file.read_text(encoding="utf-8")
    print(f"[DEBATE] Calling Agent {agent_name} (timeout={timeout}s)...")
    output = claude_code_print(prompt, context_file, timeout=timeout)
    out_path = workspace / output_name
    out_path.write_text(output, encoding="utf-8")
    ok = bool(output.strip())
    print(f"[DEBATE] Agent {agent_name} done ({len(output)} chars, ok={ok})")
    return agent_name, ok


def run_agent_debate(task: Task, workspace: Path) -> bool:
    """并行调用三个 Agent，支持重试与降级为 Architect Only"""
    max_retries = int(os.environ.get("AGENT_DEBATE_MAX_RETRY", "1"))

    for attempt in range(max_retries + 1):
        print(f"[DEBATE] Attempt {attempt + 1}/{max_retries + 1} for {task.task_id}")
        if _try_debate(task, workspace):
            return True
        if attempt < max_retries:
            print(f"[DEBATE] Retrying in 5s...")
            time.sleep(5)

    # 重试耗尽，尝试降级为 Architect Only
    print(f"[DEBATE] All retries exhausted, falling back to Architect Only")
    return _run_architect_only_fallback(task, workspace)


def _try_debate(task: Task, workspace: Path) -> bool:
    """单次 Debate 调用"""
    build_debate_prompts(task, workspace)
    build_debate_context_file(workspace)

    agents = [
        ("architect", workspace / "architect_proposal.md", "architect_proposal_output.md"),
        ("figma", workspace / "figma_audit.md", "figma_audit_output.md"),
        ("guardian", workspace / "guardian_review.md", "guardian_review_output.md"),
    ]
    context_file = workspace / "claude_context.md"
    deadline = time.time() + DEBATE_TIMEOUT
    results: dict[str, bool] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        for agent_name, prompt_file, output_name in agents:
            remaining = max(30, int(deadline - time.time()))
            per_agent_timeout = min(AGENT_TIMEOUT, remaining)
            futures.append(
                pool.submit(
                    _run_single_debate_agent,
                    agent_name, prompt_file, output_name, context_file, workspace, per_agent_timeout,
                )
            )
        try:
            for fut in as_completed(futures, timeout=DEBATE_TIMEOUT):
                if time.time() > deadline:
                    break
                name, ok = fut.result()
                results[name] = ok
        except Exception as e:
            print(f"[DEBATE] Timeout or error: {e}")
            return False

    if len(results) < 3 or not all(results.values()):
        print(f"[DEBATE] Incomplete or empty outputs: {results}")
        return False
    return True


def _run_architect_only_fallback(task: Task, workspace: Path) -> bool:
    """
    Debate 失败后的降级策略：仅运行 Architect Agent，
    将其输出直接作为最终方案写入 consensus.md，跳过 Guardian/Figma 审查。
    """
    print(f"[DEBATE FALLBACK] Running Architect Only for {task.task_id}")
    # 重新构建 Architect prompt（已存在则复用）
    build_debate_prompts(task, workspace)
    context_file = workspace / "claude_context.md"
    prompt_file = workspace / "architect_proposal.md"
    output_file = "architect_proposal_output.md"

    _, ok = _run_single_debate_agent(
        "architect", prompt_file, output_file, context_file, workspace, AGENT_TIMEOUT
    )
    if not ok:
        print(f"[DEBATE FALLBACK] Architect Only also failed")
        return False

    # 生成简化版 consensus.md
    architect_output = (workspace / "architect_proposal_output.md").read_text(encoding="utf-8")
    fallback_consensus = f"""# consensus.md — 降级方案（Debate 失败，Architect Only）

## 任务概述
- 需求: {task.raw_requirement}
- 等级: {task.level}
- 降级原因: Multi-Agent Debate 超时或失败，回退至 Architect 单 Agent

## 技术方案 (Architect Only)
{architect_output[:4000]}

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
    print(f"[DEBATE FALLBACK] consensus.md generated from Architect Only ({len(fallback_consensus)} chars)")
    return True


def build_debate_context_file(workspace: Path):
    """辩论阶段上下文：规范 + asset_analysis + Figma colors（完整 context 在编码前再构建）"""
    parts = []
    if CLAUDE_MD_PATH.exists():
        parts.append(CLAUDE_MD_PATH.read_text(encoding="utf-8"))
    analysis = workspace / "asset_analysis.json"
    if analysis.exists():
        parts.append(f"\n## Local Code-First Asset Analysis\n```json\n{analysis.read_text(encoding='utf-8')}\n```\n")
    colors = workspace / "figma" / "colors.json"
    if colors.exists():
        parts.append(f"\n## Figma Colors\n```json\n{colors.read_text(encoding='utf-8')}\n```\n")
    (workspace / "claude_context.md").write_text("\n".join(parts), encoding="utf-8")


def build_consensus_prompt(task: Task, workspace: Path) -> str:
    """构造 Consensus Agent 的 prompt"""
    architect = (workspace / "architect_proposal_output.md").read_text(encoding="utf-8")
    figma = (workspace / "figma_audit_output.md").read_text(encoding="utf-8")
    guardian = (workspace / "guardian_review_output.md").read_text(encoding="utf-8")

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


GUARDIAN_CONSTRAINT_PATTERNS = [
    (r"禁止.*新依赖|禁止.*第三方|不允许.*新依赖|不允许.*第三方|No new dependency|No third-party", "禁止新依赖"),
    (r"必须使用\s+SiteCapsRegistry|SiteCapsRegistry", "必须使用 SiteCapsRegistry"),
    (r"UIState.*不可变|immutable data class|UIState.*data class", "UIState 不可变"),
    (r"collectAsStateWithLifecycle|必须使用.*collectAsStateWithLifecycle", "collectAsStateWithLifecycle"),
    (r"TextUtils\.equals|必须使用.*TextUtils", "TextUtils.equals"),
    (r"ImmutableList|kotlinx\.collections\.immutable", "ImmutableList"),
    (r"禁止空\s*try-catch|空\s*try-catch", "禁止空 try-catch"),
]


def _extract_guardian_constraints(guardian_text: str) -> list[dict]:
    """从 Guardian 输出中提取结构化约束"""
    constraints = []
    for pattern, label in GUARDIAN_CONSTRAINT_PATTERNS:
        if re.search(pattern, guardian_text, re.IGNORECASE):
            constraints.append({"label": label, "pattern": pattern})
    return constraints


def _check_architect_against_constraints(architect_text: str, constraints: list[dict]) -> list[dict]:
    """检查 Architect 方案是否违反 Guardian 约束"""
    violations = []
    text_lower = architect_text.lower()

    for c in constraints:
        label = c["label"]
        if label == "禁止新依赖":
            if any(k in text_lower for k in ("新增依赖", "引入库", "第三方库")) or "libs.versions.toml" in architect_text:
                violations.append({"constraint": label, "detail": "Architect 方案提及新增依赖，但 Guardian 禁止"})
        elif label == "必须使用 SiteCapsRegistry":
            if "sitecapsregistry" not in text_lower and "siterules" not in text_lower:
                violations.append({"constraint": label, "detail": "Architect 方案未提及 SiteCapsRegistry 或 SiteRules"})
        elif label == "UIState 不可变":
            if "mutablestate" in text_lower or ("var " in text_lower and "uistate" in text_lower):
                violations.append({"constraint": label, "detail": "Architect 方案中 UIState 似乎包含可变状态"})
        elif label == "collectAsStateWithLifecycle":
            if "collectasstatewithlifecycle" not in text_lower and "collectasstate()" in text_lower:
                violations.append({"constraint": label, "detail": "Architect 方案使用了 collectAsState() 而非 collectAsStateWithLifecycle()"})
        elif label == "TextUtils.equals":
            if "textutils.equals" not in text_lower and ("enname ==" in text_lower or 'enname.equals' in text_lower):
                violations.append({"constraint": label, "detail": "Architect 方案中对 enName 的比较未使用 TextUtils.equals()"})
        elif label == "ImmutableList":
            if "immutablelist" not in text_lower and ("list<" in text_lower or "mutablelist" in text_lower):
                violations.append({"constraint": label, "detail": "Architect 方案中列表状态未使用 ImmutableList"})
        elif label == "禁止空 try-catch":
            if re.search(r"try\s*\{\s*\}\s*catch", architect_text, re.IGNORECASE):
                violations.append({"constraint": label, "detail": "Architect 方案中存在空 try-catch"})

    return violations


def _validate_consensus(workspace: Path) -> tuple[bool, list[dict], str]:
    """
    结构化冲突校验：提取 Guardian 约束，与 Architect 方案交叉验证。
    返回 (是否通过, 冲突列表, 校验报告文本)。
    """
    guardian_file = workspace / "guardian_review_output.md"
    architect_file = workspace / "architect_proposal_output.md"
    if not guardian_file.exists() or not architect_file.exists():
        return True, [], "Guardian 或 Architect 输出缺失，跳过校验"

    guardian_text = guardian_file.read_text(encoding="utf-8")
    architect_text = architect_file.read_text(encoding="utf-8")

    constraints = _extract_guardian_constraints(guardian_text)
    if not constraints:
        return True, [], "Guardian 未输出结构化约束，跳过校验"

    violations = _check_architect_against_constraints(architect_text, constraints)
    report_lines = [f"## Consensus Validation Report\n", f"提取到 {len(constraints)} 条 Guardian 约束:"]
    for c in constraints:
        report_lines.append(f"- ✅ {c['label']}")
    if violations:
        report_lines.append(f"\n发现 {len(violations)} 处冲突:")
        for v in violations:
            report_lines.append(f"- ❌ [{v['constraint']}] {v['detail']}")
    else:
        report_lines.append("\n未检测到冲突，Guardian 约束全部满足。")
    report = "\n".join(report_lines)
    return len(violations) == 0, violations, report


def run_consensus(task: Task, workspace: Path) -> bool:
    """运行 Consensus Agent，生成最终方案，并进行结构化冲突校验"""
    for attempt in range(CONSENSUS_MAX_RETRY + 1):
        print(f"[CONSENSUS] Attempt {attempt + 1}")
        consensus_prompt = build_consensus_prompt(task, workspace)
        consensus_output = claude_code_print(consensus_prompt, workspace / "claude_context.md")

        if not consensus_output.strip():
            print(f"[CONSENSUS] Empty output, retrying...")
            continue

        (workspace / "consensus.md").write_text(consensus_output, encoding="utf-8")
        print(f"[CONSENSUS] Done ({len(consensus_output)} chars)")

        # 结构化冲突校验
        passed, violations, report = _validate_consensus(workspace)
        (workspace / "consensus_validation.md").write_text(report, encoding="utf-8")
        if not passed:
            print(f"[CONSENSUS] Validation failed with {len(violations)} violations")
            if attempt >= CONSENSUS_MAX_RETRY:
                print("[CONSENSUS] Max retry reached, forcing L2 gate")
                # 强制转为 L2，等待人工核准
                task.level = "L2"
                save_task(task)
                return True  # 返回 True 让上层进入 waiting_gate
            continue
        print("[CONSENSUS] Validation passed")
        return True

    return False


def run_cmd(cmd, cwd=None, timeout=None, capture=True):
    cwd = cwd or PROJECT_ROOT
    print(f"[CMD] {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=capture, text=True,
            timeout=timeout, env=os.environ.copy()
        )
        if capture and result.stdout:
            print(result.stdout[-3000:])
        if result.returncode != 0 and capture and result.stderr:
            print(result.stderr[-2000:])
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] {' '.join(cmd)}")
        return -1, "", "timeout"


def get_current_branch() -> str:
    ret, stdout, _ = run_cmd(["git", "branch", "--show-current"], capture=True)
    return stdout.strip() if ret == 0 else "main"


def create_agent_branch(task_id: str, base_branch: str) -> str:
    branch = f"feature/agent-{task_id}"
    run_cmd(["git", "checkout", base_branch])
    run_cmd(["git", "pull", "origin", base_branch])
    run_cmd(["git", "checkout", "-b", branch])
    return branch


def switch_site_if_needed(site_hint: str) -> bool:
    """切换 active site，返回是否发生了实际切换"""
    if not site_hint:
        return False
    configs = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    if not configs.exists():
        return False
    content = configs.read_text(encoding="utf-8")
    match = re.search(r'(val\s+site\s*:\s*Site\s*=\s*)(\w+)', content)
    if not match:
        return False
    current = match.group(2)
    # 去掉 Debug/Release 后缀后再比较，避免子串误匹配
    current_stem = current
    for suffix in ("Debug", "Release"):
        if current_stem.endswith(suffix):
            current_stem = current_stem[: -len(suffix)]
            break
    hint_stem = site_hint
    for suffix in ("Debug", "Release"):
        if hint_stem.endswith(suffix):
            hint_stem = hint_stem[: -len(suffix)]
            break
    if hint_stem.lower() == current_stem.lower():
        return False
    site_dir = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "site"
    candidates = [f.stem for f in site_dir.glob("*.kt")
                  if hint_stem.lower() == f.stem.lower() and f.stem not in ("Site", "SiteChannels")]
    if not candidates:
        return False
    target = candidates[0] + "Debug"
    new_content = re.sub(r'(val\s+site\s*:\s*Site\s*=\s*)\w+', rf'\g<1>{target}', content)
    configs.write_text(new_content, encoding="utf-8")
    print(f"[SITE] Switched: {current} -> {target}")
    return True


def _collect_rag_candidates() -> list[Path]:
    """收集所有可能的 RAG 候选文件路径：main/java + 所有 uiStyle 层级 + sport/ 模块"""
    dirs_to_scan: list[Path] = []

    # 1. 主代码源集
    main_java = PROJECT_ROOT / "app" / "src" / "main" / "java"
    if main_java.exists():
        dirs_to_scan.append(main_java)

    # 2. 所有 uiStyle 层级（多站点特定代码）
    uiStyle_root = PROJECT_ROOT / "app" / "src" / "uiStyle"
    if uiStyle_root.exists():
        for first_level in uiStyle_root.iterdir():
            if not first_level.is_dir():
                continue
            for sub in first_level.rglob("java"):
                if sub.is_dir():
                    dirs_to_scan.append(sub)

    # 3. sport/ 共享模块
    sport_java = PROJECT_ROOT / "sport" / "src" / "main" / "java"
    if sport_java.exists():
        dirs_to_scan.append(sport_java)

    candidates: list[Path] = []
    for d in dirs_to_scan:
        for f in d.rglob("*.kt"):
            if "Screen" in f.name or "ViewModel" in f.name or "Contract" in f.name or "UseCase" in f.name:
                candidates.append(f)
    return candidates


def build_rag_context(requirement: str = "") -> str:
    """RAG：关键词/图谱检索 + 最近修改的 Screen/ViewModel/Contract/UseCase Few-Shot"""
    examples = []
    seen = set()

    if requirement:
        for rel in semantic_search_files(requirement, limit=3):
            full = PROJECT_ROOT / rel
            if full.exists() and rel not in seen:
                seen.add(rel)
                try:
                    content = full.read_text(encoding="utf-8")[:1500]
                    examples.append(f"### {rel} (keyword/graph match)\n```kotlin\n{content}\n```")
                except Exception:
                    pass

    # 统一收集所有源集下的候选文件
    candidates = _collect_rag_candidates()
    # 按最近修改时间排序
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    for c in candidates:
        rel = str(c.relative_to(PROJECT_ROOT)).replace("\\", "/")
        if rel in seen:
            continue
        seen.add(rel)
        try:
            content = c.read_text(encoding="utf-8")[:1500]
            examples.append(f"### {rel}\n```kotlin\n{content}\n```")
        except Exception:
            pass
        if len(examples) >= 8:
            break

    if examples:
        return "\n## Project Best Practices (Auto-Retrieved)\n\n" + "\n\n".join(examples)
    return ""


def build_context_file(task: Task, workspace: Path):
    """构造 Claude 上下文文件 — V3 版：增加 RAG + Consensus + Asset Map + Code-First Analysis"""
    context_parts = []
    # 1. 项目规范
    if CLAUDE_MD_PATH.exists():
        context_parts.append(CLAUDE_MD_PATH.read_text(encoding="utf-8"))
    # 2. RAG 最佳实践
    rag_context = build_rag_context(task.raw_requirement)
    if rag_context:
        context_parts.append(rag_context)
    # 3. 需求
    context_parts.append(f"\n\n## Current Task\n- ID: {task.task_id}\n- Level: {task.level}\n- Requirement: {task.raw_requirement}\n")
    # 4. 本地代码资产分析（代码优先）
    asset_analysis = workspace / "asset_analysis.json"
    if asset_analysis.exists():
        context_parts.append(
            f"\n## Local Code-First Asset Analysis\n"
            f"```json\n{asset_analysis.read_text(encoding='utf-8')}\n```\n\n"
            f"> 规则：编码前先阅读以上分析。如果接口已有图片 URL 字段，使用 Coil AsyncImage 动态加载；"
            f"如果本地已有可复用图标，直接引用本地 drawable；只有真正缺失的静态图才引用 Asset Map 中的新资产。\n"
        )
    # 5. Consensus 方案
    consensus_file = workspace / "consensus.md"
    if consensus_file.exists():
        context_parts.append(f"\n## Consensus Plan\n```markdown\n{consensus_file.read_text(encoding='utf-8')[:3000]}\n```\n")
    platform_site = workspace / "platform_site.json"
    if platform_site.exists():
        context_parts.append(
            f"\n## Platform Figma Site (platform-figma-list)\n```json\n"
            f"{platform_site.read_text(encoding='utf-8')}\n```\n"
        )
    # 6. 视觉资产映射
    asset_map = workspace / "asset_map.json"
    if asset_map.exists():
        context_parts.append(f"\n## Asset Map\n```json\n{asset_map.read_text(encoding='utf-8')}\n```\n")
    # 7. Figma 原始资产
    figma_dir = workspace / "figma"
    if figma_dir.exists():
        colors_file = figma_dir / "colors.json"
        if colors_file.exists():
            context_parts.append(f"\n## Figma Colors\n```json\n{colors_file.read_text()}\n```\n")
    # 8. 之前构建失败的错误信息
    if task.error_log:
        context_parts.append(f"\n## Previous Build Errors\n```\n{task.error_log[:2000]}\n```\n")

    context_file = workspace / "claude_context.md"
    context_file.write_text("\n".join(context_parts), encoding="utf-8")
    return context_file


def claude_code_print(prompt: str, context_file: Path, timeout: Optional[int] = None) -> str:
    """调用 claude --print 非交互模式"""
    env = os.environ.copy()
    ctx = ""
    if context_file.exists():
        ctx = context_file.read_text(encoding="utf-8")
    full_prompt = f"""
[项目上下文]
{ctx}

[当前指令]
{prompt}
"""
    cmd = ["claude", "--print"]
    run_timeout = timeout if timeout is not None else CLAUDE_TIMEOUT
    print(f"[CLAUDE] Prompt length: {len(full_prompt)} chars, timeout={run_timeout}s")
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=run_timeout, env=env, cwd=str(PROJECT_ROOT)
        )
        print(f"[CLAUDE] Exit code: {result.returncode}")
        return result.stdout or ""
    except subprocess.TimeoutExpired:
        print(f"[CLAUDE TIMEOUT] after {run_timeout}s")
        return ""
    except Exception as e:
        print(f"[CLAUDE ERROR] {e}")
        return ""


# 安全黑名单：Claude 禁止修改的目录/文件模式
BLOCKED_PATHS = [
    ".github/",
    "jg_tools/",
    "benchmark/",
    "gradle/wrapper/gradle-wrapper.properties",
    # buildSrc 下除 site/ 和 Dependencies.kt/Version.kt/Utils.kt 外，Configs.kt 由 Orchestrator 控制
    "buildSrc/src/main/kotlin/Configs.kt",
    # 加密/密钥相关
    "keystore/",
    ".jks",
    ".keystore",
    # 禁止覆盖主题注册 KSP 生成文件
    "SiteThemeRegistryGenerated",
    # 禁止修改 CI/CD 配置
    ".github/workflows/",
    # 禁止修改 apk 保护脚本
    "jg_tools/protect.sh",
    "jg_tools/shell/",
    # 禁止修改 baseline profile（由构建脚本自动同步）
    "app/src/main/baselineProfiles/",
]


def _is_blocked_path(file_path: str) -> tuple[bool, str]:
    """检查文件路径是否在黑名单中，返回 (是否被拦截, 原因)"""
    # 统一使用正斜杠匹配
    normalized = file_path.replace("\\", "/")
    for pattern in BLOCKED_PATHS:
        if pattern in normalized:
            return True, f"命中安全黑名单: {pattern}"
    # 额外拦截：BuildConfig 生成逻辑或加密密钥文件
    if "BuildConfig" in normalized and normalized.endswith(".kt"):
        return True, "禁止修改 BuildConfig 生成逻辑"
    if "encrypt" in normalized.lower() and "key" in normalized.lower():
        return True, "禁止修改加密密钥相关文件"
    return False, ""


def apply_code_changes(claude_output: str):
    """解析 Claude 输出中的 === FILE: path === 块，写入文件系统"""
    lines = claude_output.splitlines()
    i = 0
    applied = []
    blocked = []
    while i < len(lines):
        match = FILE_MARKER.match(lines[i])
        if match:
            file_path = match.group(1).strip()
            i += 1
            content_lines = []
            while i < len(lines) and lines[i].strip() != END_MARKER:
                content_lines.append(lines[i])
                i += 1
            full_path = (PROJECT_ROOT / file_path).resolve()
            # 路径遍历防护：禁止写出 PROJECT_ROOT 之外
            if not str(full_path).startswith(str(PROJECT_ROOT)):
                print(f"[APPLY SKIP] 非法路径 (路径遍历): {file_path}")
                i += 1
                continue
            # 安全黑名单校验
            is_blocked, reason = _is_blocked_path(file_path)
            if is_blocked:
                print(f"[APPLY BLOCKED] {file_path} | {reason}")
                blocked.append((file_path, reason))
                i += 1
                continue
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text("\n".join(content_lines), encoding="utf-8")
            applied.append(file_path)
            print(f"[APPLY] {file_path}")
        i += 1

    # 如果有被拦截的文件，记录到安全审计日志
    if blocked:
        security_log = PROJECT_ROOT / "AICodeAgent" / "data" / "security_violations.log"
        security_log.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat()
        with security_log.open("a", encoding="utf-8") as f:
            for bp, br in blocked:
                f.write(f"[{timestamp}] BLOCKED: {bp} | {br}\n")
        print(f"[SECURITY] {len(blocked)} 个文件被拦截，详见 security_violations.log")

    return applied


def run_gradle_build(task: Task) -> tuple[int, str]:
    """运行 Gradle 构建和测试，返回 (exit_code, log)"""
    log_path = WORKSPACE_ROOT / task.task_id / "build.log"
    # 1. assembleDebug
    exit_code, stdout, stderr = run_cmd(
        ["./gradlew", "app:assembleDebug", "--console=plain"],
        timeout=BUILD_TIMEOUT
    )
    log_content = f"=== assembleDebug ===\n{stdout}\n{stderr}\n"
    if exit_code != 0:
        log_path.write_text(log_content, encoding="utf-8")
        return exit_code, log_content
    # 2. testDebugUnitTest
    exit_code, stdout, stderr = run_cmd(
        ["./gradlew", "testDebugUnitTest", "--console=plain"],
        timeout=BUILD_TIMEOUT
    )
    log_content += f"\n=== testDebugUnitTest ===\n{stdout}\n{stderr}\n"
    if exit_code != 0:
        log_path.write_text(log_content, encoding="utf-8")
        return exit_code, log_content
    # 3. lintDebug
    exit_code, stdout, stderr = run_cmd(
        ["./gradlew", "lintDebug", "--console=plain"],
        timeout=BUILD_TIMEOUT
    )
    log_content += f"\n=== lintDebug ===\n{stdout}\n{stderr}\n"
    log_path.write_text(log_content, encoding="utf-8")
    return exit_code, log_content


def parse_gradle_errors(log: str) -> str:
    """调用 course_correct.py 统一解析"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False, encoding='utf-8') as f:
        f.write(log)
        log_file = f.name
    try:
        ret, stdout, _ = run_cmd(
            [sys.executable, str(PROJECT_ROOT / "AICodeAgent" / "scripts" / "course_correct.py"),
             "--log", log_file, "--format", "json"],
            capture=True
        )
        if ret == 0:
            return stdout
    finally:
        os.unlink(log_file)
    # 兜底：返回最后 30 行
    return "\n".join(log.splitlines()[-30:])


def build_codex_fix_prompt(requirement: str, review_report: str, attempt: int) -> str:
    return f"""
Codex 逻辑审查未通过（第 {attempt} 次修复）

原始需求: {requirement}

审查报告（必须逐条修复 Logic issues / Regression risks / Suggested fixes）:
{review_report[:6000]}

修复规则:
1. 仅修改与需求和审查意见直接相关的文件
2. 修复逻辑漏洞与回归风险，不要破坏其他站点/既有 case
3. site enName 比较必须使用 TextUtils.equals()
4. UIState 保持不可变 data class
5. 不要运行 Gradle 或 git
6. 不要引入新的第三方依赖

请输出修复后的代码，使用 === FILE: path === 格式。
"""


def build_acceptance_fix_prompt(requirement: str, review_report: str, attempt: int) -> str:
    return f"""
需求验收审查未通过（第 {attempt} 次修复）

原始需求（必须完整满足）:
{requirement}

验收审查报告:
{review_report[:6000]}

修复规则:
1. 对照 Requirement coverage 逐条补全或修正实现
2. 修复 Logic issues / Performance issues 中指出的明显问题
3. 仅改相关文件；不要跑 Gradle/git；不要新增第三方依赖
4. site enName 用 TextUtils.equals()；UIState 不可变

请输出修复代码，=== FILE: path === 格式。
"""


def _parse_intake_json(claude_output: str) -> dict:
    m = re.search(r"```json\s*(\{.*?\})\s*```", claude_output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[^{}]*\"needs_clarification\"[^{}]*\}", claude_output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def assess_requirement_clarity(task: Task, workspace: Path) -> tuple[bool, list[str], str]:
    """需求 intake：不明确则返回 (True, questions, reason)"""
    if os.environ.get("AGENT_SKIP_CLARIFICATION", "0") == "1":
        return False, [], "skip by env"
    # 用户已澄清续跑
    if task.resume_after_clarification:
        return False, [], "already clarified"

    ctx = build_context_file(task, workspace)
    prompt = f"""
你是需求分析员。判断以下 Android 多站点需求是否**足够明确**，可进入 Architect/Figma/QA 三方技术辩论。

需求:
{task.raw_requirement}

等级: {task.level} | 站点 hint: {task.site_hint or "未指定"}

若缺少以下任一类信息，必须反问用户（needs_clarification=true）:
- 目标页面/模块或文件路径
- 目标站点（enName）或是否全站
- 验收标准 / 交互细节
- 是否涉及 Figma/主题/资源
- 与既有功能的边界（改什么不改什么）

若需求已是 L0 级明确小改（如「在 xxx strings.xml 增加 key foo」）且信息完备，可 needs_clarification=false。

严格输出 JSON（可包在 ```json 代码块内）:
{{"needs_clarification": true|false, "questions": ["问题1","问题2"], "reason": "一句话"}}
最多 5 个问题。
"""
    out = claude_code_print(prompt, ctx, timeout=120)
    data = _parse_intake_json(out)
    needs = bool(data.get("needs_clarification"))
    questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
    reason = str(data.get("reason") or "")
    if needs and not questions:
        questions = ["请补充目标页面/站点与验收标准。"]
    return needs, questions[:5], reason


def maybe_wait_for_clarification(task: Task, workspace: Path) -> bool:
    """若需用户澄清则进入 waiting_clarification 并返回 True（调用方应 return）"""
    needs, questions, reason = assess_requirement_clarity(task, workspace)
    if not needs:
        return False

    lines = [f"# 需求澄清\n\n原因: {reason}\n", "## 待用户回答\n"]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q}\n")
    (workspace / "clarification_questions.md").write_text("".join(lines), encoding="utf-8")

    task.clarification_deadline = (
        datetime.now() + timedelta(hours=CLARIFICATION_TIMEOUT_HOURS)
    ).isoformat()
    save_task(task)
    transition(task.task_id, State.WAITING_CLARIFICATION, "requirement ambiguous")
    _notify_clarification(task, questions)
    return True


def _notify_clarification(task: Task, questions: list[str]):
    chat_id = task.chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    qs = "\n".join(f"• {q}" for q in questions)
    msg = (
        f"<b>需求待澄清</b>\n任务ID: <code>{task.task_id}</code>\n"
        f"需求: {task.raw_requirement[:100]}\n\n{qs}\n\n"
        f"请回复: <code>/reply {task.task_id} 你的回答</code>\n"
        f"或 Web POST /api/reply"
    )
    if chat_id and token:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
            return
        except Exception as e:
            print(f"[TELEGRAM clarify] {e}")
    print(f"[CLARIFY NOTIFY] {msg}")


def build_fix_prompt(requirement: str, errors: str, attempt: int) -> str:
    return f"""
构建/测试失败（第 {attempt} 次重试）

原始需求: {requirement}

Gradle 错误摘要:
{errors}

修复规则:
1. 仅修改与当前需求直接相关的文件
2. 如果是测试中的 Context 问题，使用 Robolectric RuntimeEnvironment.getApplication()
3. 如果是资源缺失，在 strings.xml 或对应 siteRes 下补充
4. 如果是 import 错误，检查包名和依赖
5. 不要引入新的第三方依赖
6. 不要运行任何 Gradle 命令
7. 不要执行 git 命令

请输出修复后的代码，使用以下格式:
=== FILE: path/to/File.kt ===
[完整文件内容]
=== END FILE ===
"""


def _extract_committable_files(workspace: Path) -> list[str]:
    """从 consensus.md 提取文件清单，只提交被 Agent 明确修改/新增的文件"""
    files: list[str] = []
    consensus = workspace / "consensus.md"
    if consensus.exists():
        text = consensus.read_text(encoding="utf-8")
        # 匹配表格行中的文件路径（支持 .kt .xml .gradle .kts .json .md .properties .pro 等）
        for line in text.splitlines():
            if "|" not in line or line.strip().startswith("| --") or line.strip().startswith("|---"):
                continue
            # 取表格第一列作为文件路径
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if not cols:
                continue
            candidate = cols[0]
            # 匹配常见的项目内路径模式
            if re.search(r"(?:app|buildSrc|sport|benchmark|site-caps-ksp)/[\w./-]+\.[\w]+", candidate):
                files.append(candidate)
    # 补充 asset_map.json 里映射到的新增 drawable / strings
    asset_map = workspace / "asset_map.json"
    if asset_map.exists():
        try:
            data = json.loads(asset_map.read_text(encoding="utf-8"))
            for entry in data.get("assets", []):
                local = entry.get("local_file")
                if local and entry.get("action") in ("ingested_vector", "ingested_raster"):
                    # 构造相对路径：保守地加入所有可能的 siteRes / main/res 路径
                    files.append(f"app/src/main/res/drawable/{local}")
                    files.append(f"app/src/siteRes/*/drawable/{local}")
        except json.JSONDecodeError:
            pass
    # 去重并保持顺序
    seen = set()
    result = []
    for f in files:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


DEPENDENCY_FILES = [
    "gradle/libs.versions.toml",
    "build.gradle.kts",
    "app/build.gradle.kts",
    "app/build.gradle",
    "sport/build.gradle.kts",
    "sport/build.gradle",
    "buildSrc/build.gradle.kts",
    "buildSrc/build.gradle",
    "site-caps-ksp/build.gradle.kts",
    "theme-registry-ksp/build.gradle.kts",
]


def _check_dependency_diff(base_branch: str) -> tuple[bool, str]:
    """
    检查依赖文件是否有变更。返回 (是否有新增依赖, 变更摘要)。
    如果有变更但 consensus.md 未明确批准，则视为违规。
    """
    changed = []
    for dep_file in DEPENDENCY_FILES:
        full = PROJECT_ROOT / dep_file
        if not full.exists():
            continue
        # 获取 base branch 上的版本
        ret, base_content, _ = run_cmd(
            ["git", "show", f"{base_branch}:{dep_file}"],
            capture=True,
        )
        if ret != 0:
            # base branch 可能无此文件，视为新增
            base_content = ""
        current_content = full.read_text(encoding="utf-8")
        if base_content != current_content:
            changed.append(dep_file)

    if not changed:
        return False, ""

    summary = f"检测到依赖文件变更: {', '.join(changed)}"
    return True, summary


def git_commit(task_id: str, workspace: Path, base_branch: str = ""):
    """只提交 consensus.md 中列出的文件，避免污染人工临时改动；提交前检查依赖变更"""
    # 依赖变更检测
    dep_changed, dep_summary = _check_dependency_diff(base_branch or get_current_branch())
    if dep_changed:
        consensus = workspace / "consensus.md"
        approved = False
        if consensus.exists():
            text = consensus.read_text(encoding="utf-8").lower()
            # consensus 中需明确提及新依赖/第三方库/依赖变更
            if any(k in text for k in ("新依赖", "第三方库", "依赖", "新增库", "引入库")):
                approved = True
        if not approved:
            violation_msg = (
                f"[DEP BLOCK] 依赖变更未在 consensus.md 中明确批准。\n{dep_summary}\n"
                "请在 consensus.md 中补充对新依赖的说明后重试，或降级为 L2 人工核准。"
            )
            print(violation_msg)
            (workspace / "dependency_violation.md").write_text(violation_msg, encoding="utf-8")
            raise RuntimeError("dependency_change_not_approved")

    files = _extract_committable_files(workspace)
    if not files:
        print("[GIT WARN] consensus.md 中无文件清单，回退到 git add -A")
        run_cmd(["git", "add", "-A"])
    else:
        # 先 reset 掉未跟踪/未暂存的改动（保留已跟踪文件），避免之前任务的残留
        run_cmd(["git", "checkout", "--", "."])
        # 逐个 add；通配符在 Python 层面展开（subprocess 不会走 shell）
        for f in files:
            if "*" in f:
                matched = list(PROJECT_ROOT.glob(f))
                for m in matched:
                    rel = str(m.relative_to(PROJECT_ROOT)).replace("\\", "/")
                    run_cmd(["git", "add", "--", rel])
            else:
                run_cmd(["git", "add", "--", f])
        # 如果还有未 add 的新增文件（consensus 没列全），兜底 add 未被跟踪但位于 app/ 下的文件
        run_cmd(["git", "add", "--", "app/", "buildSrc/", "sport/"])
    run_cmd(["git", "commit", "-m", f"feat: automated implementation [agent-{task_id}]"])


def _detect_dependency_violation(workspace: Path) -> str:
    """在 apply_code_changes 后扫描是否有被拦截的依赖文件被修改（兜底检测）"""
    # 检查工作区 git diff 中是否包含依赖文件
    ret, diff_out, _ = run_cmd(
        ["git", "diff", "--name-only"],
        capture=True,
    )
    if ret != 0:
        return ""
    changed = [line.strip() for line in diff_out.splitlines() if line.strip()]
    for dep_file in DEPENDENCY_FILES:
        if dep_file in changed:
            return f"[DEP VIOLATION] {dep_file} 被修改但未经共识批准"
    return ""


def git_push(branch: str):
    run_cmd(["git", "push", "origin", branch])


def create_pr(task_id: str, requirement: str, branch: str, base_branch: str = "", deviation_report: str = "") -> str:
    ret, _, _ = run_cmd(["gh", "--version"], capture=True)
    if ret != 0:
        return ""
    base = (base_branch or "main").strip()
    title = f"[Agent] {requirement[:50]}"
    body = f"""## Automated Implementation
- Task ID: {task_id}
- Requirement: {requirement}
### Verification
- [x] `./gradlew app:assembleDebug` SUCCESS
- [x] `./gradlew testDebugUnitTest` PASS
- [x] `./gradlew lintDebug` PASS
> 自动生成，请 Review 后 Merge
"""
    if deviation_report and deviation_report.strip():
        body += f"\n### Consensus Deviation Audit\n{deviation_report}\n"
    ret, stdout, _ = run_cmd(
        ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch],
        capture=True
    )
    if ret == 0:
        for line in stdout.splitlines():
            if line.startswith("https://github.com/"):
                return line.strip()
    return ""


def notify_telegram(task: Task, pr_url: str = ""):
    chat_id = task.chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        return
    try:
        import requests
        status_emoji = "✅" if task.current_state == State.COMPLETED.value else "❌"
        msg = (
            f"<b>{status_emoji} Android Agent</b>\n"
            f"任务ID: <code>{task.task_id}</code>\n"
            f"状态: {task.current_state}\n"
            f"需求: {task.raw_requirement[:80]}\n"
        )
        if pr_url:
            msg += f"PR: {pr_url}\n"
        if task.error_log and task.current_state == State.FAILED.value:
            msg += f"错误: <pre>{task.error_log[:400]}</pre>\n"
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")


def auto_level(requirement: str) -> str:
    req = requirement.lower()
    l0 = ["文案", "颜色", "间距", "字体大小", "string", "bug 修复", "修复崩溃", "typo"]
    if any(k in req for k in l0):
        return "L0"
    l2 = ["重构", "架构", "状态机", "跨模块", "核心", "网络层", "域名", "数据库迁移", "全局"]
    if any(k in req for k in l2):
        return "L2"
    return "L1"


def needs_figma(requirement: str) -> bool:
    return any(k in requirement.lower() for k in FIGMA_KEYWORDS)


def analyze_local_screens_and_apis(requirement: str, site_hint: str = "") -> dict:
    """
    写页面前，先读本地同类页面 + 接口，判断需要下载什么图片。
    返回 asset_analysis.json 结构，供 Figma Auditor 精准定位资产。
    """
    analysis = {
        "screen_patterns": [],      # 同类页面的图片加载模式
        "api_image_fields": [],     # 接口中已有的图片字段
        "local_drawables": [],      # 本地已有的图标/图片名
        "missing_gaps": [],         # 与 Figma 对比后推断的缺口
    }

    # 1. 扫描本地同类 Screen 文件（基于需求关键词匹配文件名）
    keywords = [kw.lower() for kw in requirement.split() if len(kw) > 2]
    screen_dir = PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "sport"
    for screen_file in screen_dir.rglob("*Screen*.kt"):
        name_lower = screen_file.name.lower()
        if any(kw in name_lower for kw in keywords):
            content = screen_file.read_text(encoding="utf-8")
            # 提取图片引用模式
            patterns = {
                "async_image": "AsyncImage" in content,
                "image_vector": "imageVector" in content,
                "painter_resource": "painterResource" in content,
                "coil": "rememberAsyncImagePainter" in content or "AsyncImage" in content,
            }
            analysis["screen_patterns"].append({
                "file": str(screen_file.relative_to(PROJECT_ROOT)),
                "patterns": patterns,
            })
            # 提取本地 drawable 引用（R.drawable.xxx 或 painterResource(R.drawable.xxx)）
            for m in re.finditer(r"R\.drawable\.(\w+)", content):
                analysis["local_drawables"].append(m.group(1))

    # 2. 扫描 API / Repository / UseCase / DTO，找图片 URL 字段
    api_dirs = [
        PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "sport",
    ]
    for api_dir in api_dirs:
        if not api_dir.exists():
            continue
        for api_file in api_dir.rglob("*.kt"):
            if any(x in api_file.name for x in ["Api", "Repository", "UseCase", "Dto", "Model"]):
                content = api_file.read_text(encoding="utf-8")
                # 用正则直接匹配属性声明，避免注释/字符串字面量中的字段名被误匹配
                for m in re.finditer(
                    r"val\s+([a-zA-Z_]\w*(?:[Uu]rl|[Uu]RI|[Ll]ink))\s*:\s*(?:String|Uri|URL|HttpUrl|OkHttpUrl)",
                    content,
                ):
                    field_name = m.group(1)
                    # 仅保留常见图片/媒体相关字段名
                    if any(k in field_name for k in ["icon", "avatar", "image", "logo", "banner", "pic", "photo", "thumb", "cover"]):
                        analysis["api_image_fields"].append({
                            "file": str(api_file.relative_to(PROJECT_ROOT)),
                            "field": field_name,
                        })

    # 去重
    analysis["local_drawables"] = sorted(set(analysis["local_drawables"]))
    return analysis


def resolve_task_platform_site(task: Task, workspace: Path) -> Optional[PlatformSiteResolved]:
    """从 site_hint 解析 platform-figma-list，写入 workspace/platform_site.json"""
    if not task.site_hint:
        return None
    resolved = resolve_site_hint(task.site_hint)
    if not resolved:
        print(f"[FIGMA] 未在 platform-figma-list 匹配站点: {task.site_hint}")
        return None
    if resolved.error:
        print(f"[FIGMA] 站点 {task.site_hint}: {resolved.error}")
    else:
        print(
            f"[FIGMA] 站点 {task.site_hint} → {resolved.cn_name} "
            f"(fileKey={resolved.file_key}, enName={resolved.en_name})"
        )
    write_platform_site_meta(workspace, resolved)
    if resolved.en_name and not task.site_hint:
        task.site_hint = resolved.en_name
        save_task(task)
    return resolved


def write_asset_analysis(task: Task, workspace: Path) -> dict:
    analysis_path = workspace / "asset_analysis.json"
    if analysis_path.exists():
        return json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis = analyze_local_screens_and_apis(task.raw_requirement, task.site_hint)
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"[ASSET] 本地代码分析: screens={len(analysis['screen_patterns'])}, "
        f"api_fields={len(analysis['api_image_fields'])}, drawables={len(analysis['local_drawables'])}"
    )
    return analysis


def fetch_figma_assets(task: Task, workspace: Path, platform: Optional[PlatformSiteResolved] = None):
    """拉取 Figma 颜色/站点资产到 workspace/figma（辩论前调用，站点来自 platform-figma-list）"""
    figma_dir = workspace / "figma"
    figma_dir.mkdir(exist_ok=True)

    platform = platform or resolve_task_platform_site(task, workspace)
    site_query = task.site_hint or ""
    en_name = platform.en_name if platform else ""

    if FIGMA_FETCH_SCRIPT.exists() and site_query:
        run_cmd(
            ["bash", str(FIGMA_FETCH_SCRIPT), site_query, str(figma_dir), en_name or ""],
            cwd=str(PROJECT_ROOT),
            timeout=300,
        )
        return

    figma_tools = PROJECT_ROOT / "figma-tools"
    if not figma_tools.exists():
        return
    if site_query:
        run_cmd(
            ["npm", "run", "download:site", "--", "--platform-site", site_query],
            cwd=str(figma_tools),
            timeout=300,
        )
        if en_name:
            multi_assets = figma_tools / "output" / ".multi-site" / en_name / "assets"
            if multi_assets.exists():
                dest = figma_dir / "assets"
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(multi_assets, dest)
            multi_colors = figma_tools / "output" / ".multi-site" / en_name / "colors" / "colors.json"
            if multi_colors.exists():
                shutil.copy(multi_colors, figma_dir / "colors.json")
    else:
        run_cmd(["npm", "run", "colors:download"], cwd=str(figma_tools), timeout=120)
        colors_src = figma_tools / "output" / "colors.json"
        if colors_src.exists():
            shutil.copy(colors_src, figma_dir / "colors.json")


def prepare_asset_context(task: Task, workspace: Path):
    """辩论前：Code-First 分析 +（按需）Figma 拉取"""
    write_asset_analysis(task, workspace)
    platform = resolve_task_platform_site(task, workspace) if task.site_hint else None
    if needs_figma(task.raw_requirement) or task.site_hint:
        fetch_figma_assets(task, workspace, platform=platform)


def build_asset_map(workspace: Path):
    """根据 asset_analysis + figma_audit_output 生成 asset_map.json"""
    assets = []
    analysis_path = workspace / "asset_analysis.json"
    if analysis_path.exists():
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        for drawable in analysis.get("local_drawables", []):
            assets.append({
                "figma_node": drawable,
                "local_file": f"{drawable}.xml",
                "action": "reuse_local",
                "decision_reason": "同类页面已引用 R.drawable." + drawable,
            })
        for field_info in analysis.get("api_image_fields", []):
            assets.append({
                "figma_node": field_info.get("field", "image_url"),
                "local_file": None,
                "action": "use_api_url",
                "decision_reason": f"接口字段 {field_info.get('field')} @ {field_info.get('file')}",
            })

    figma_out = workspace / "figma_audit_output.md"
    if figma_out.exists():
        for line in figma_out.read_text(encoding="utf-8").splitlines():
            if "|" not in line or line.strip().startswith("|--") or "Figma 节点" in line:
                continue
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) < 4:
                continue
            node, local_status, decision, reason = cols[0], cols[1], cols[2], cols[3] if len(cols) > 3 else ""
            if node.startswith("（"):
                continue
            action = "reuse_local" if "复用" in decision else "download" if "下载" in decision else "spec_only"
            assets.append({
                "figma_node": node,
                "local_file": local_status if ".xml" in local_status else None,
                "action": action,
                "decision_reason": reason or decision,
            })

    asset_map = {"assets": assets}
    (workspace / "asset_map.json").write_text(
        json.dumps(asset_map, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[ASSET] asset_map.json written ({len(assets)} entries)")
    return asset_map


def write_unrecoverable_error(workspace: Path, requirement: str, errors: str):
    (workspace / "unrecoverable_error.md").write_text(
        f"# Unrecoverable Error\n\n## Requirement\n{requirement}\n\n## Errors\n```\n{errors[:4000]}\n```\n",
        encoding="utf-8",
    )


def generate_docs(task: Task, workspace: Path):
    (workspace / "intent.md").write_text(
        f"# intent.md\n\n## 任务分级\n- 等级: {task.level}\n- 判定依据: auto\n\n"
        f"## 任务信息\n- 目标: {task.raw_requirement}\n- 成功标准: Gradle 全绿\n",
        encoding="utf-8"
    )
    if task.level in ("L1", "L2"):
        (workspace / "design.md").write_text(
            f"# design.md\n\n## 核心设计\n- 需求: {task.raw_requirement}\n",
            encoding="utf-8"
        )
    (workspace / "plan.md").write_text(
        f"# plan.md\n\n## 实施顺序\n1. 编码 -> 2. 构建 -> 3. 修复 -> 4. 提交\n",
        encoding="utf-8"
    )


def bootstrap_l0_consensus(task: Task, workspace: Path):
    """L0 轻量任务：不写辩论 Agent，生成最小 consensus 供编码阶段使用"""
    (workspace / "consensus.md").write_text(
        f"# consensus.md — L0 快速方案\n\n"
        f"## 任务概述\n"
        f"- 需求: {task.raw_requirement}\n"
        f"- 等级: L0（跳过 Multi-Agent Debate）\n\n"
        f"## 技术方案\n"
        f"- 按需求直接修改目标文件，保持现有代码风格\n"
        f"- 站点: {task.site_hint or _get_current_site_name() or '当前 Configs.site'}\n\n"
        f"## 最终文件清单\n"
        f"| 文件 | 操作 | 说明 |\n"
        f"|------|------|------|\n"
        f"| （见需求） | 修改 | Claude 根据需求推断路径并输出 === FILE: === 块 |\n",
        encoding="utf-8",
    )


def _get_current_site_name() -> str:
    """从 Configs.kt 提取当前 active site 的 enName（小写，去 Debug/Release 后缀）"""
    configs = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    if not configs.exists():
        return ""
    content = configs.read_text(encoding="utf-8")
    match = re.search(r'val\s+site\s*:\s*Site\s*=\s*(\w+)', content)
    if not match:
        return ""
    raw = match.group(1)
    # 去掉 Debug / Release 后缀
    for suffix in ("Debug", "Release"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.lower()


def _extract_consensus_file_paths(workspace: Path) -> list[str]:
    """从 consensus.md 提取所有文件路径"""
    files: list[str] = []
    consensus = workspace / "consensus.md"
    if not consensus.exists():
        return files
    text = consensus.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "|" not in line or line.strip().startswith("| --") or line.strip().startswith("|---"):
            continue
        cols = [c.strip() for c in line.split("|") if c.strip()]
        if not cols:
            continue
        candidate = cols[0]
        if re.search(r"(?:app|buildSrc|sport|benchmark|site-caps-ksp)/[\w./-]+\.[\w]+", candidate):
            files.append(candidate)
    return files


def _validate_site_awareness(workspace: Path, task: Task) -> list[str]:
    """校验 consensus.md 中的文件路径是否与当前 active site 匹配，返回警告列表"""
    warnings: list[str] = []
    current_site = _get_current_site_name()
    if not current_site:
        return warnings

    files = _extract_consensus_file_paths(workspace)
    for f in files:
        # siteRes 目录校验：siteRes/{enName}/
        m = re.search(r"siteRes/([^/]+)/", f)
        if m:
            dir_site = m.group(1).lower()
            if dir_site not in ("main", current_site) and current_site not in dir_site and dir_site not in current_site:
                warnings.append(f"[SITE WARN] 文件 {f} 位于 siteRes/{dir_site}/，但当前 active site 是 {current_site}")

        # uiStyle 目录校验：uiStyle/NOT_HOME/xxx/ 或 uiStyle/HOME/xxx/
        m = re.search(r"uiStyle/[^/]+/([^/]+)/", f)
        if m:
            dir_style = m.group(1).lower()
            if dir_style not in ("java", current_site) and current_site not in dir_style and dir_style not in current_site:
                warnings.append(f"[SITE WARN] 文件 {f} 位于 uiStyle/.../{dir_style}/，但当前 active site 是 {current_site}")

    return warnings


def _generate_consensus_deviation(workspace: Path, task: Task) -> str:
    """编码后审计：对比 consensus.md 承诺 vs 实际修改，生成 deviation report"""
    consensus_path = workspace / "consensus.md"
    promised_files: set[str] = set()
    if consensus_path.exists():
        promised_files = set(_extract_consensus_file_paths(workspace))

    actual_files: set[str] = set()
    base = task.base_branch or "main"
    ret, diff_out, _ = run_cmd(
        ["git", "diff", "--name-only", base],
        capture=True,
    )
    if ret == 0:
        actual_files = {line.strip() for line in diff_out.splitlines() if line.strip()}

    missing = promised_files - actual_files
    extra = actual_files - promised_files
    lines: list[str] = []

    if missing:
        lines.append("### ⚠️ 未完成的共识文件")
        for f in sorted(missing):
            lines.append(f"- [ ] {f} （consensus 中提及但未修改）")
        lines.append("")

    if extra:
        lines.append("### ⚠️ 超出共识范围的修改")
        for f in sorted(extra):
            lines.append(f"- [ ] {f} （实际修改但 consensus 未提及）")
        lines.append("")

    sw_path = workspace / "site_warnings.md"
    if sw_path.exists():
        lines.append("### 🏷️ Site 感知警告")
        lines.append(sw_path.read_text(encoding="utf-8"))
        lines.append("")

    dep_violation = _detect_dependency_violation(workspace)
    if dep_violation:
        lines.append("### 🔒 依赖变更警告")
        lines.append(dep_violation)
        lines.append("")

    if not lines:
        lines.append("✅ 编码结果与 consensus 一致，无偏差。")

    report = "\n".join(lines)
    (workspace / "consensus_deviation.md").write_text(report, encoding="utf-8")
    print(f"[AUDIT] consensus_deviation.md: {len(missing)} missing, {len(extra)} extra")
    return report


def run_coding_build_pr(task: Task, ws: Path):
    """编码 → Gradle → git → PR（L2 核准后或 L0/L1 辩论完成后执行）"""
    # 编码前 site 校验
    site_warnings = _validate_site_awareness(ws, task)
    if site_warnings:
        for w in site_warnings:
            print(w)
        # 将警告写入工作区，供后续审计
        (ws / "site_warnings.md").write_text("\n".join(site_warnings), encoding="utf-8")

    asset_map = build_asset_map(ws)
    platform_meta = None
    meta_file = ws / "platform_site.json"
    if meta_file.exists():
        platform_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    effective_en = (platform_meta or {}).get("enName") or task.site_hint
    file_key = (platform_meta or {}).get("fileKey") or os.environ.get("FIGMA_FILE_KEY", "")
    process_visual_assets(
        task.raw_requirement,
        ws,
        site_hint=effective_en or task.site_hint,
        file_key=file_key,
        merge_existing_map=asset_map,
    )
    site_switched = False
    if effective_en:
        site_switched = switch_site_if_needed(effective_en)
    elif task.site_hint:
        site_switched = switch_site_if_needed(task.site_hint)

    # 如果发生了 site 切换，首次构建前执行 clean，避免增量编译携带旧 site 缓存
    if site_switched:
        print("[BUILD] Site switched, running gradle clean first...")
        clean_code, clean_out, clean_err = run_cmd(
            ["./gradlew", "clean", "--console=plain"],
            timeout=BUILD_TIMEOUT,
        )
        if clean_code != 0:
            print(f"[BUILD WARN] gradle clean failed: {clean_err[:500]}")
        else:
            print("[BUILD] gradle clean done")

    if not task.branch:
        base_branch = task.base_branch or get_current_branch()
        task.base_branch = base_branch
        task.branch = create_agent_branch(task.task_id, base_branch)
        save_task(task)
    elif not task.base_branch:
        task.base_branch = get_current_branch()
        save_task(task)

    context_file = build_context_file(task, ws)
    initial_prompt = f"""
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

    build_passed = False
    attempt = 0
    codex_round = 0
    acceptance_round = 0

    while attempt <= task.max_retries:
        transition(task.task_id, State.CODING, f"attempt {attempt + 1}")
        claude_output = claude_code_print(initial_prompt, context_file)

        if not claude_output.strip():
            task.error_log = "Claude returned empty output"
            save_task(task)
            if attempt >= task.max_retries:
                transition(task.task_id, State.FAILED, "empty claude output")
                write_unrecoverable_error(ws, task.raw_requirement, task.error_log)
                notify_telegram(task)
                return
            attempt += 1
            continue

        applied = apply_code_changes(claude_output)
        print(f"[APPLY] {len(applied)} files modified")

        transition(task.task_id, State.BUILDING, f"attempt {attempt + 1}")
        exit_code, log = run_gradle_build(task)

        if exit_code != 0:
            if attempt >= task.max_retries:
                task.error_log = parse_gradle_errors(log)
                save_task(task)
                write_unrecoverable_error(ws, task.raw_requirement, task.error_log)
                transition(task.task_id, State.FAILED, "max retries exceeded")
                notify_telegram(task)
                return
            transition(task.task_id, State.CORRECTING, f"build fail attempt {attempt + 1}")
            errors = parse_gradle_errors(log)
            task.error_log = errors
            save_task(task)
            context_file = build_context_file(task, ws)
            initial_prompt = build_fix_prompt(task.raw_requirement, errors, attempt + 2)
            print(f"[CORRECT] Gradle retry {attempt + 2}")
            attempt += 1
            continue

        print("[BUILD] All checks passed — starting Codex logic review")
        transition(task.task_id, State.CODEX_REVIEW, f"codex round {codex_round + 1}")
        passed, review_report = run_codex_review(
            task.raw_requirement, ws, base_branch=task.base_branch or ""
        )
        (ws / "codex_review.md").write_text(review_report, encoding="utf-8")

        if not passed:
            codex_round += 1
            print(f"[CODEX] Review FAIL (round {codex_round}/{CODEX_REVIEW_MAX_RETRY})")
            task.error_log = review_report[:4000]
            save_task(task)
            if codex_round > CODEX_REVIEW_MAX_RETRY:
                write_unrecoverable_error(ws, task.raw_requirement, task.error_log)
                transition(task.task_id, State.FAILED, "codex review max retries")
                notify_telegram(task)
                return
            transition(task.task_id, State.CORRECTING, f"codex fail round {codex_round}")
            context_file = build_context_file(task, ws)
            initial_prompt = build_codex_fix_prompt(
                task.raw_requirement, review_report, codex_round + 1
            )
            attempt += 1
            continue

        print("[CODEX] Review PASS — starting requirement acceptance review")
        transition(task.task_id, State.REQUIREMENT_REVIEW, f"acceptance round {acceptance_round + 1}")
        acc_passed, acc_report = run_requirement_acceptance_review(
            task.raw_requirement, ws, base_branch=task.base_branch or ""
        )
        (ws / "requirement_review.md").write_text(acc_report, encoding="utf-8")

        if acc_passed:
            print("[ACCEPTANCE] Requirement review PASS")
            build_passed = True
            break

        acceptance_round += 1
        print(
            f"[ACCEPTANCE] FAIL (round {acceptance_round}/{ACCEPTANCE_REVIEW_MAX_RETRY})"
        )
        task.error_log = acc_report[:4000]
        save_task(task)
        if acceptance_round > ACCEPTANCE_REVIEW_MAX_RETRY:
            write_unrecoverable_error(ws, task.raw_requirement, task.error_log)
            transition(task.task_id, State.FAILED, "requirement review max retries")
            notify_telegram(task)
            return
        transition(task.task_id, State.CORRECTING, f"acceptance fail round {acceptance_round}")
        context_file = build_context_file(task, ws)
        initial_prompt = build_acceptance_fix_prompt(
            task.raw_requirement, acc_report, acceptance_round + 1
        )
        attempt += 1

    if not build_passed:
        transition(task.task_id, State.FAILED, "coding loop ended without success")
        notify_telegram(task)
        return

    # 编码后审计：对比 consensus 与实际修改
    deviation_report = _generate_consensus_deviation(ws, task)

    transition(task.task_id, State.GIT_COMMITTING, "build passed")
    git_commit(task.task_id, ws)
    git_push(task.branch)

    transition(task.task_id, State.CREATING_PR, "pushed")
    pr_url = create_pr(task.task_id, task.raw_requirement, task.branch, task.base_branch, deviation_report)
    task.pr_url = pr_url
    save_task(task)

    transition(task.task_id, State.NOTIFYING, "pr created" if pr_url else "pr skipped")
    notify_telegram(task, pr_url)
    transition(task.task_id, State.COMPLETED, "all done")
    print(f"[DONE] {task.task_id}")


def _snapshot_git_state() -> tuple[str, list[tuple[str, str]]]:
    """返回 (当前分支, git status 快照列表[(status, path), ...])"""
    _, branch_out, _ = run_cmd(["git", "branch", "--show-current"], capture=True)
    current_branch = branch_out.strip()
    _, status_out, _ = run_cmd(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    snapshot = []
    for line in status_out.splitlines():
        if len(line) >= 3:
            status = line[:2]
            path = line[3:].strip()
            snapshot.append((status, path))
    return current_branch, snapshot


def _has_uncommitted_work() -> bool:
    """检测工作区是否存在未提交的 tracked 文件修改（排除纯 untracked）"""
    _, status_out, _ = run_cmd(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    for line in status_out.splitlines():
        if len(line) >= 3:
            status = line[:2]
            # M, A, D, R, C 表示 tracked 文件有变更；?? 表示 untracked
            if status != "??":
                return True
    return False


def _restore_task_environment(task: Task, original_branch: str, snapshot: list[tuple[str, str]], original_configs: str):
    """任务结束后恢复环境：切回 base branch、删除 agent 分支、恢复 Configs.kt、清理 untracked 残留。
    安全模式：检测到人工未提交更改时，拒绝 stash/checkout/drop，降级为软清理。"""
    print(f"[CLEANUP] Restoring environment after task {task.task_id}")
    base = task.base_branch or original_branch or "main"

    # 安全模式检查：如果存在非 untracked 的未提交修改，可能是人工正在编辑
    if _has_uncommitted_work():
        print("[CLEANUP SAFETY] Detected uncommitted work — skipping destructive stash/checkout/drop.")
        print("[CLEANUP SAFETY] Manual cleanup may be required for the following files:")
        _, status_out, _ = run_cmd(
            ["git", "status", "--short"],
            capture=True,
        )
        for line in status_out.splitlines():
            print(f"  {line}")

        # 软清理：仅恢复 Configs.kt（site 切换可能修改了它）
        configs = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
        if configs.exists() and original_configs:
            current = configs.read_text(encoding="utf-8")
            if current != original_configs:
                configs.write_text(original_configs, encoding="utf-8")
                print("[CLEANUP] Restored Configs.kt")

        # 尝试删除本地 agent 分支（如果存在且不是当前分支）
        agent_branch = task.branch
        if agent_branch:
            _, branch_out, _ = run_cmd(["git", "branch", "--show-current"], capture=True)
            if branch_out.strip() != agent_branch:
                run_cmd(["git", "branch", "-D", agent_branch])
                print(f"[CLEANUP] Removed agent branch {agent_branch}")
            else:
                print(f"[CLEANUP SAFETY] Cannot remove current branch {agent_branch}; please switch manually")

        print("[CLEANUP] Soft cleanup done — environment NOT fully restored due to uncommitted work")
        return

    # 1. 先 stash 当前未提交的更改（防止 checkout 失败），然后切回 base branch
    run_cmd(["git", "stash", "push", "--include-untracked", "-m", f"agent-cleanup-{task.task_id}"])
    run_cmd(["git", "checkout", base])

    # 2. 删除本地 agent 分支（如果存在且不是当前分支）
    agent_branch = task.branch
    if agent_branch:
        run_cmd(["git", "branch", "-D", agent_branch])

    # 3. 恢复 Configs.kt（site 切换可能修改了它）
    configs = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    if configs.exists() and original_configs:
        current = configs.read_text(encoding="utf-8")
        if current != original_configs:
            configs.write_text(original_configs, encoding="utf-8")
            print("[CLEANUP] Restored Configs.kt")

    # 4. 恢复 tracked 文件到 base branch 状态
    run_cmd(["git", "checkout", "--", "."])

    # 5. 清理新增的 untracked 文件（snapshot 中不存在的）
    _, status_out, _ = run_cmd(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    snapshot_paths = {p for s, p in snapshot}
    for line in status_out.splitlines():
        if len(line) >= 3 and line.startswith("??"):
            path = line[3:].strip()
            if path not in snapshot_paths:
                target = PROJECT_ROOT / path
                if target.is_file():
                    target.unlink()
                    print(f"[CLEANUP] Removed untracked file: {path}")
                elif target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                    print(f"[CLEANUP] Removed untracked dir: {path}")

    # 6. 清理刚才的 stash
    run_cmd(["git", "stash", "drop"])

    print("[CLEANUP] Done")


def process_task(task: Task):
    """主处理流程 — V3"""
    print(f"\n{'='*60}\n[PROCESS] {task.task_id} | {task.level} | {task.raw_requirement[:60]}\n{'='*60}")

    ws = WORKSPACE_ROOT / task.task_id
    ws.mkdir(parents=True, exist_ok=True)

    # 保存环境快照（Configs.kt + git 状态）
    configs_path = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    original_configs = configs_path.read_text(encoding="utf-8") if configs_path.exists() else ""
    original_branch, git_snapshot = _snapshot_git_state()

    try:
        # L2 核准后续跑：跳过 planning / debate / consensus
        if task.resume_from_gate:
            print(f"[RESUME] L2 gate approved, skip to coding phase")
            task.resume_from_gate = 0
            save_task(task)
            transition(task.task_id, State.PLANNING, "resume after L2 gate")
            run_coding_build_pr(task, ws)
            return

        resume_clarified = bool(task.resume_after_clarification)
        if resume_clarified:
            print("[RESUME] User clarification received, skip intake")
            task.resume_after_clarification = 0
            save_task(task)

        if task.level == "auto":
            task.level = auto_level(task.raw_requirement)
            save_task(task)

        if not resume_clarified or not (ws / "requirement.md").exists():
            generate_docs(task, ws)
        transition(task.task_id, State.PLANNING, "docs generated")

        if not resume_clarified or not (ws / "asset_analysis.md").exists():
            prepare_asset_context(task, ws)

        # 需求不明确：先反问用户，再三方辩论（L0 同样可走澄清，除非已澄清续跑）
        if not resume_clarified and maybe_wait_for_clarification(task, ws):
            return

        # L0：跳过三方辩论，直接进入编码（节省 claude 调用）
        if task.level == "L0":
            transition(task.task_id, State.DEBATING, "L0 fast path (skip agents)")
            transition(task.task_id, State.CONSENSUS, "L0 minimal consensus")
            bootstrap_l0_consensus(task, ws)
            build_asset_map(ws)
            run_coding_build_pr(task, ws)
            return

        transition(task.task_id, State.DEBATING, "starting multi-agent debate")
        if not run_agent_debate(task, ws):
            task.error_log = "Debate stage failed or timed out"
            save_task(task)
            transition(task.task_id, State.CORRECTING, "debate timeout")
            transition(task.task_id, State.FAILED, "debate timeout or error")
            notify_telegram(task)
            return

        transition(task.task_id, State.CONSENSUS, "building consensus")
        if not run_consensus(task, ws):
            task.error_log = "Consensus stage failed"
            save_task(task)
            transition(task.task_id, State.FAILED, "consensus generation failed")
            notify_telegram(task)
            return

        build_asset_map(ws)

        if task.level == "L2":
            task.gate_deadline = (datetime.now() + timedelta(hours=24)).isoformat()
            save_task(task)
            transition(task.task_id, State.WAITING_GATE, "L2 gate waiting for /continue")
            _notify_l2_gate(task)
            return

        run_coding_build_pr(task, ws)
    finally:
        # 无论成功/失败/L2 等待，都恢复环境（L2 等待时也需要恢复，因为编码阶段还未开始）
        _restore_task_environment(task, original_branch, git_snapshot, original_configs)


def _notify_l2_gate(task: Task):
    """L2 辩论+共识完成后通知人工核准"""
    chat_id = task.chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        notify_telegram(task)
        return
    try:
        import requests
        msg = (
            f"<b>L2 任务等待人工核准</b>\n"
            f"任务ID: <code>{task.task_id}</code>\n"
            f"需求: {task.raw_requirement[:120]}\n"
            f"共识方案已生成于 workspace/{task.task_id}/consensus.md\n"
            f"请回复: <code>/continue {task.task_id}</code> 开始编码"
        )
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[NOTIFY L2 ERROR] {e}")



def resume_l2_task(task_id: str) -> bool:
    """L2 核准：重新入队，由 Executor 调用 process_task(resume_from_gate)"""
    return approve_gate_resume(task_id)
