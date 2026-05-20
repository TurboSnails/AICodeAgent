#!/usr/bin/env python3
"""
Headless Orchestrator V3
- Multi-Agent Debate + Consensus
- Code-First asset_analysis 在辩论前注入
- L2 gate 核准后 resume_from_gate 续跑编码阶段
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
    """并行调用三个 Agent，整体与单 Agent 均有超时"""
    print(f"[DEBATE] Starting multi-agent debate for {task.task_id}")
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


def run_consensus(task: Task, workspace: Path) -> bool:
    """运行 Consensus Agent，生成最终方案"""
    for attempt in range(CONSENSUS_MAX_RETRY + 1):
        print(f"[CONSENSUS] Attempt {attempt + 1}")
        consensus_prompt = build_consensus_prompt(task, workspace)
        consensus_output = claude_code_print(consensus_prompt, workspace / "claude_context.md")

        if not consensus_output.strip():
            print(f"[CONSENSUS] Empty output, retrying...")
            continue

        (workspace / "consensus.md").write_text(consensus_output, encoding="utf-8")
        print(f"[CONSENSUS] Done ({len(consensus_output)} chars)")
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


def switch_site_if_needed(site_hint: str):
    if not site_hint:
        return
    configs = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
    if not configs.exists():
        return
    content = configs.read_text(encoding="utf-8")
    match = re.search(r'(val\s+site\s*:\s*Site\s*=\s*)(\w+)', content)
    if not match:
        return
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
        return
    site_dir = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "site"
    candidates = [f.stem for f in site_dir.glob("*.kt")
                  if hint_stem.lower() == f.stem.lower() and f.stem not in ("Site", "SiteChannels")]
    if not candidates:
        return
    target = candidates[0] + "Debug"
    new_content = re.sub(r'(val\s+site\s*:\s*Site\s*=\s*)\w+', rf'\g<1>{target}', content)
    configs.write_text(new_content, encoding="utf-8")
    print(f"[SITE] Switched: {current} -> {target}")


def build_rag_context(requirement: str = "") -> str:
    """RAG：关键词/图谱检索 + 最近修改的 Screen/ViewModel Few-Shot"""
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

    java_dir = PROJECT_ROOT / "app" / "src" / "main" / "java"
    if java_dir.exists():
        candidates = sorted(
            [f for f in java_dir.rglob("*.kt") if "Screen" in f.name or "ViewModel" in f.name],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
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
            if len(examples) >= 5:
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


def apply_code_changes(claude_output: str):
    """解析 Claude 输出中的 === FILE: path === 块，写入文件系统"""
    lines = claude_output.splitlines()
    i = 0
    applied = []
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
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text("\n".join(content_lines), encoding="utf-8")
            applied.append(file_path)
            print(f"[APPLY] {file_path}")
        i += 1
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


def git_commit(task_id: str, workspace: Path):
    """只提交 consensus.md 中列出的文件，避免污染人工临时改动"""
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


def git_push(branch: str):
    run_cmd(["git", "push", "origin", branch])


def create_pr(task_id: str, requirement: str, branch: str, base_branch: str = "") -> str:
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
    if effective_en:
        switch_site_if_needed(effective_en)
    elif task.site_hint:
        switch_site_if_needed(task.site_hint)

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

    for attempt in range(task.max_retries + 1):
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
            continue

        applied = apply_code_changes(claude_output)
        print(f"[APPLY] {len(applied)} files modified")

        transition(task.task_id, State.BUILDING, f"attempt {attempt + 1}")
        exit_code, log = run_gradle_build(task)

        if exit_code == 0:
            print("[BUILD] All checks passed")
            break

        if attempt >= task.max_retries:
            task.error_log = parse_gradle_errors(log)
            save_task(task)
            write_unrecoverable_error(ws, task.raw_requirement, task.error_log)
            transition(task.task_id, State.FAILED, "max retries exceeded")
            notify_telegram(task)
            return

        transition(task.task_id, State.CORRECTING, f"attempt {attempt + 1}")
        errors = parse_gradle_errors(log)
        task.error_log = errors
        save_task(task)
        context_file = build_context_file(task, ws)
        initial_prompt = build_fix_prompt(task.raw_requirement, errors, attempt + 2)
        print(f"[CORRECT] Retry {attempt + 2} with fix prompt")

    transition(task.task_id, State.GIT_COMMITTING, "build passed")
    git_commit(task.task_id, ws)
    git_push(task.branch)

    transition(task.task_id, State.CREATING_PR, "pushed")
    pr_url = create_pr(task.task_id, task.raw_requirement, task.branch, task.base_branch)
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


def _restore_task_environment(task: Task, original_branch: str, snapshot: list[tuple[str, str]], original_configs: str):
    """任务结束后恢复环境：切回 base branch、删除 agent 分支、恢复 Configs.kt、清理 untracked 残留"""
    print(f"[CLEANUP] Restoring environment after task {task.task_id}")
    base = task.base_branch or original_branch or "main"

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

        if task.level == "auto":
            task.level = auto_level(task.raw_requirement)
            save_task(task)

        generate_docs(task, ws)
        transition(task.task_id, State.PLANNING, "docs generated")

        prepare_asset_context(task, ws)

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
