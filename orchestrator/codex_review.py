#!/usr/bin/env python3
"""
构建全绿后的 Codex / Claude 逻辑审查：
- 逻辑正确性与漏洞
- 对既有 case / 流程的回归影响（结合 graph_bridge 影响面）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from graph_bridge import get_impact_summary, extract_files_from_consensus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODEX_TIMEOUT = int(os.environ.get("CODEX_REVIEW_TIMEOUT", "900"))
CODEX_CMD = os.environ.get("CODEX_CMD", "").strip()  # 例: "codex exec --full-auto"


def _run_cmd(cmd: list, cwd: Path, input_text: str = "", timeout: int = CODEX_TIMEOUT) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            input=input_text or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            env=os.environ.copy(),
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


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


def parse_codex_verdict(output: str) -> bool:
    """True = PASS"""
    if not output or not output.strip():
        return False
    text = output.strip()
    # 显式 FAIL 优先
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
    # 兜底：含 FAIL 且无 PASS
    if "FAIL" in text.upper() and "PASS" not in text.upper():
        return False
    return False


def codex_review_print(prompt: str, context_text: str) -> str:
    """
    调用 Codex CLI；未安装或失败时回退 claude --print（同一审查 prompt）。
    环境变量 CODEX_CMD：完整命令前缀，如 `codex exec -a never`。
    """
    full = f"[审查上下文]\n{context_text[:50000]}\n\n[审查指令]\n{prompt}"
    if CODEX_CMD:
        parts = CODEX_CMD.split()
        code, out, err = _run_cmd(parts, PROJECT_ROOT, input_text=full, timeout=CODEX_TIMEOUT)
        if code == 0 and out.strip():
            print(f"[CODEX] {CODEX_CMD} ok, {len(out)} chars")
            return out
        print(f"[CODEX] {CODEX_CMD} failed ({code}): {err[:300]}")

    # 尝试 PATH 中的 codex（常见子命令 exec）
    for cmd in (
        ["codex", "exec", "-a", "never", "--"],
        ["codex", "exec", "--"],
    ):
        try:
            probe = subprocess.run(["which", "codex"], capture_output=True, text=True)
            if probe.returncode != 0:
                break
        except Exception:
            break
        code, out, err = _run_cmd(cmd + [full[:8000]], PROJECT_ROOT, timeout=CODEX_TIMEOUT)
        if code == 0 and out.strip():
            print(f"[CODEX] {' '.join(cmd)} ok")
            return out

    print("[CODEX] fallback to claude --print (Codex reviewer persona)")
    try:
        r = subprocess.run(
            ["claude", "--print"],
            input=full,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT,
            cwd=str(PROJECT_ROOT),
            env=os.environ.copy(),
        )
        return r.stdout or ""
    except Exception as e:
        print(f"[CODEX/claude fallback] {e}")
        return ""


def run_codex_review(
    requirement: str,
    workspace: Path,
    base_branch: str = "",
) -> Tuple[bool, str]:
    """
    执行审查，返回 (passed, full_report_markdown)。
    """
    changed = list_changed_files(base_branch)
    if not changed:
        changed = extract_files_from_consensus(workspace)
    impact = get_impact_summary(changed) if changed else ""

    ctx_parts = []
    for name in ("consensus.md", "asset_map.json", "site_warnings.md"):
        p = workspace / name
        if p.exists():
            ctx_parts.append(f"### {name}\n{p.read_text(encoding='utf-8')[:4000]}\n")
    context_text = "\n".join(ctx_parts)

    prompt = build_codex_review_prompt(requirement, workspace, changed, impact)
    raw = codex_review_print(prompt, context_text)
    report = f"# Codex Logic Review\n\n{raw}\n" if raw else "# Codex Logic Review\n\n(empty output — treat as FAIL)\n"
    passed = parse_codex_verdict(raw)
    return passed, report
