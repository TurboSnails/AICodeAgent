#!/usr/bin/env python3
"""
审查阶段共享工具函数 — 从 orchestrator/codex_review.py 迁移
提供 prompt 构建、verdict 解析、文件变更读取等纯逻辑函数。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from utils.paths import PROJECT_ROOT


def list_changed_files(base_branch: str = "") -> List[str]:
    """当前工作区相对 base 的变更文件列表"""
    if base_branch:
        code, out, _ = _run_cmd(
            ["git", "diff", "--name-only", base_branch, "HEAD"],
            PROJECT_ROOT,
            timeout=60,
        )
        if code == 0 and out.strip():
            return [ln.strip() for ln in out.splitlines() if ln.strip()]
    code, out, _ = _run_cmd(
        ["git", "diff", "--name-only", "HEAD"],
        PROJECT_ROOT,
        timeout=60,
    )
    if code == 0 and out.strip():
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    code, out, _ = _run_cmd(
        ["git", "status", "--porcelain=v1"],
        PROJECT_ROOT,
        timeout=60,
    )
    files = []
    for line in out.splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip())
    return files


def _run_cmd(cmd: list, cwd: Path, timeout: int = 60) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def workspace_context(workspace: Path, extra_names: Optional[List[str]] = None) -> str:
    names = ["consensus.md", "asset_map.json", "site_warnings.md", "codex_review.md"]
    if extra_names:
        names = extra_names + names
    parts = []
    for name in names:
        p = workspace / name
        if p.exists():
            parts.append(f"### {name}\n{p.read_text(encoding='utf-8')[:4000]}\n")
    return "\n".join(parts)


def read_changed_sources(changed_files: List[str], max_total: int = 28000) -> str:
    """读取变更源码片段供验收审查"""
    chunks = []
    total = 0
    for rel in changed_files:
        if not rel.endswith((".kt", ".kts", ".xml")):
            continue
        path = PROJECT_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = f"### `{rel}`\n```\n{text[:6000]}\n```\n"
        if total + len(block) > max_total:
            chunks.append(f"### `{rel}`\n（已截断，文件过长）\n")
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks) if chunks else "（未能读取变更源码）"


def parse_codex_verdict(output: str) -> bool:
    """True = PASS"""
    if not output or not output.strip():
        return False
    text = output.strip()
    if re.search(r"(?i)##\s*Verdict\s*\n\s*FAIL", text):
        return False
    if re.search(r"(?i)Verdict\s*:\s*FAIL", text):
        return False
    if re.search(r"(?i)\bVerdict\b[^\n]*\bFAIL\b", text):
        return False
    if re.search(r"(?i)##\s*Verdict\s*\n\s*PASS", text):
        return True
    if re.search(r"(?i)Verdict\s*:\s*PASS", text):
        return True
    if re.search(r"(?i)\bVerdict\b[^\n]*\bPASS\b", text):
        return True
    if "FAIL" in text.upper() and "PASS" not in text.upper():
        return False
    return False


def build_codex_review_prompt(
    requirement: str,
    workspace: Path,
    changed_files: List[str],
    impact_summary: str,
) -> str:
    consensus = ""
    cp = workspace / "consensus.md"
    if cp.exists():
        consensus = cp.read_text(encoding="utf-8")[:8000]

    diff_excerpt = ""
    if changed_files:
        code, out, _ = _run_cmd(["git", "diff", "--stat"], PROJECT_ROOT, timeout=120)
        if code == 0:
            diff_excerpt = out[:12000]

    return f"""
你是 **Codex 逻辑审查员**（只读审查，禁止修改任何文件）。Gradle 构建已通过，请审查本次实现是否可合并。

## 原始需求
{requirement}

## 共识方案（节选）
{consensus[:6000] if consensus else "（无 consensus.md）"}

## 本次变更文件
{chr(10).join(f"- `{f}`" for f in changed_files[:30]) or "（未能列出 diff）"}

## 代码图谱影响面（供回归分析）
{impact_summary or "（图谱不可用）"}

## 审查维度（必须逐项覆盖）
1. **逻辑正确性**：是否满足需求与 consensus；状态机/UDF 是否一致；边界条件。
2. **逻辑漏洞**：空指针、竞态、错误分支、加密/站点判断误用（须 TextUtils.equals 比较 enName）。
3. **回归影响**：本次改动是否破坏其他站点、其他页面或既有 case；列出可能受影响的文件/流程。
4. **多站点**：若动到 SiteRules / theme / siteRes，是否误伤其他 enName。

## 输出格式（严格遵守，便于机器解析）
```markdown
## Verdict
PASS 或 FAIL

## Logic issues
- （无则写 无）

## Security / edge cases
- 

## Regression risks
- 

## Suggested fixes
- （FAIL 时必填具体改法；PASS 可写 无）
```

**判定规则**：存在任一必须修复的逻辑错误、漏洞或高概率回归 → Verdict 必须为 **FAIL**。
仅风格/命名 nit 且不影响行为 → **PASS**。
"""


def build_requirement_acceptance_prompt(
    requirement: str,
    workspace: Path,
    changed_files: List[str],
    prior_codex_report: str,
) -> str:
    consensus = ""
    cp = workspace / "consensus.md"
    if cp.exists():
        consensus = cp.read_text(encoding="utf-8")[:6000]

    code_excerpt = read_changed_sources(changed_files)

    return f"""
你是 **需求验收审查员**（只读）。Codex 逻辑审查已通过，请专注：**实现是否真正满足用户原始需求**，以及 **明显的逻辑/性能问题**。

## 用户原始需求（最高优先级，逐条对照）
{requirement}

## 共识方案（实现应对齐）
{consensus[:5000] if consensus else "（无）"}

## 上一轮 Codex 审查摘要（勿重复已 PASS 的回归项，除非仍相关）
{prior_codex_report[:2500] if prior_codex_report else "（无）"}

## 本次变更源码（节选）
{code_excerpt}

## 审查维度（必须逐项）
1. **需求符合度**：原始需求中的功能点是否全部实现？有无遗漏、做错页面/站点、与需求矛盾的实现？
2. **验收标准**：若需求含交互/文案/边界，代码是否体现？
3. **明显逻辑错误**：死代码、永远为 false 的分支、错误的状态更新、遗漏的 Event 上报等（编译已通过层面的逻辑）。
4. **明显性能问题**：主线程 IO、Composable 内重复分配大对象、无 key 的 Lazy 列表、可预见的 O(n²) 循环、未 remember 的昂贵计算等。
5. **与 consensus 偏差**：若偏离 consensus 且损害需求，判 FAIL。

## 输出格式（严格遵守）
```markdown
## Verdict
PASS 或 FAIL

## Requirement coverage
- 逐条对照原始需求：已实现 / 缺失 / 错误

## Logic issues
- 

## Performance issues
- （无则写 无）

## Suggested fixes
- （FAIL 时必填；PASS 可写 无）
```

**判定**：任一核心需求未实现、实现与需求明显不符、或存在必须修复的逻辑/性能问题 → **FAIL**。
仅极小 nit 且用户可接受 → **PASS**。
"""


def build_red_team_prompt(
    requirement: str,
    workspace: Path,
    changed_files: List[str],
    prior_codex_report: str,
) -> str:
    consensus = ""
    cp = workspace / "consensus.md"
    if cp.exists():
        consensus = cp.read_text(encoding="utf-8")[:4000]

    diff_excerpt = ""
    if changed_files:
        code, out, _ = _run_cmd(["git", "diff", "--stat"], PROJECT_ROOT, timeout=60)
        if code == 0:
            diff_excerpt = out[:6000]

    return f"""
你是 **Red Team（红队攻击者）**。你的唯一目标是找出代码中的漏洞、隐患和设计缺陷。
你不关心代码是否"能跑"，你只关心它是否会在生产环境中出问题、被利用、或产生回归。

## 原始需求
{requirement}

## 共识方案（节选）
{consensus[:3000] if consensus else "（无）"}

## 变更统计
{diff_excerpt or "（未能获取 diff）"}

## 上一轮 Codex 审查摘要
{prior_codex_report[:2000] if prior_codex_report else "（无）"}

## 攻击维度（必须逐项扫描）
1. **边界条件攻击**：空列表、极大/极小值、特殊字符输入
2. **竞态与并发**：多线程、状态同步、生命周期泄漏
3. **空指针与异常**：NPE、类型转换失败、未捕获异常
4. **安全与隐私**：硬编码敏感信息、日志泄露、权限绕过
5. **多站点兼容性**：修改是否误伤其他 `enName` 站点
6. **过度设计**：不必要的抽象、过早优化、引入未使用依赖
7. **可维护性**：魔法值、重复代码、违背项目规范

## 输出格式
```markdown
## Verdict
PASS 或 FAIL

## Attack findings
- 

## Severity
- Critical / High / Medium / Low

## Suggested fixes
- （FAIL 时必填具体防御方案）
```

**判定**：发现任一 Critical 或 High 级别安全问题、明显竞态/NPE 隐患、或多站点误伤 → **FAIL**。
仅可接受的设计取舍或已充分防御的边界 → **PASS**。
"""
