"""
逃逸检测器 — 主动识别不可解问题，提前止损
1. 不可解检测：连续 N 次相同错误模式 → 直接失败
2. 复杂度超限：文件数 / 跨模块 / 核心文件触及 → 升级 L2
3. 风险逃逸：高风险操作触发人工审查
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from utils.config_loader import cfg_int
from utils.logging_config import get_logger

logger = get_logger(__name__)

# 核心架构文件模式 — 触及这些文件时触发 L2 升级或人工审查
CORE_FILE_PATTERNS = [
    r"KoinModule\.kt",
    r"SiteRules\.kt",
    r"SiteCapsRegistry",
    r"OkHttpClientProvider",
    r"NetworkModule",
    r"DatabaseModule",
    r"AppDatabase",
    r"Migration",
    r"Dao\.kt",
    r"Entity\.kt",
    r"NavGraph",
    r"MainActivity\.kt",
    r"Application\.kt",
]

# 复杂度阈值 — 惰性读取（避免 import 时触发 IO）
def _complexity_file_threshold() -> int:
    return cfg_int("escape.file_threshold", 10)


def _unsolvable_repeat_threshold() -> int:
    return cfg_int("escape.unsolvable_repeat", 2)


def _extract_error_fingerprint(error_json: str) -> tuple[str, ...]:
    """
    从 course_correct.py 输出的 JSON 中提取错误类型指纹。
    返回排序后的错误类型元组，用于比对是否重复。
    """
    try:
        errors = json.loads(error_json)
        if not isinstance(errors, list):
            return ("unknown",)
        types = sorted({e.get("type", "unknown") for e in errors if isinstance(e, dict)})
        return tuple(types) if types else ("unknown",)
    except json.JSONDecodeError:
        # 非 JSON 错误摘要：提取前 3 个关键字
        words = re.findall(r"[A-Z][a-zA-Z]+", error_json)
        return tuple(sorted(set(words[:5]))) if words else ("unknown",)


def detect_unsolvable(error_history: list[str]) -> tuple[bool, str]:
    """
    不可解检测。
    :param error_history: 历次失败的错误解析结果（字符串列表）
    :return: (是否不可解, 原因说明)
    """
    threshold = _unsolvable_repeat_threshold()
    if len(error_history) < threshold + 1:
        return False, ""

    # 取最近 N+1 次错误
    recent = error_history[-(threshold + 1):]
    fingerprints = [_extract_error_fingerprint(e) for e in recent]

    # 检查最近 N 次（不包括第一次）是否全部相同
    target = fingerprints[-1]
    repeat_count = sum(1 for f in fingerprints[1:] if f == target)

    if repeat_count >= threshold:
        reason = (
            f"连续 {repeat_count} 次构建/审查失败均命中相同的错误模式: "
            f"{', '.join(target)}。Claude 似乎陷入循环，无法自行修复。"
        )
        logger.warning(f"[ESCAPE] Unsolvable detected: {reason}")
        return True, reason

    return False, ""


def assess_complexity(consensus_text: str) -> dict:
    """
    评估 consensus.md 中的方案复杂度。
    :return: {"level": "L0|L1|L2", "file_count": int, "cross_module": bool,
              "touches_core": bool, "reasons": [str]}
    """
    reasons: list[str] = []
    files = _extract_consensus_files(consensus_text)
    file_count = len(files)
    threshold = _complexity_file_threshold()

    # 文件数量
    if file_count > threshold:
        reasons.append(f"涉及 {file_count} 个文件（阈值 {threshold}）")

    # 跨模块检测
    modules = set()
    for f in files:
        for mod in ("app/", "buildSrc/", "sport/", "benchmark/", "site-caps-ksp/"):
            if f.startswith(mod):
                modules.add(mod.rstrip("/"))
    cross_module = len(modules) > 1
    if cross_module:
        reasons.append(f"跨模块修改: {', '.join(sorted(modules))}")

    # 核心文件检测
    core_hits = []
    for f in files:
        for pattern in CORE_FILE_PATTERNS:
            if re.search(pattern, f):
                core_hits.append(f"{f} 匹配 {pattern}")
    if core_hits:
        reasons.append(f"触及核心架构文件: {', '.join(core_hits[:3])}")

    # 判定等级
    if core_hits or cross_module or file_count > threshold:
        level = "L2"
    elif file_count > 5:
        level = "L1"
    else:
        level = "L0"

    return {
        "level": level,
        "file_count": file_count,
        "cross_module": cross_module,
        "touches_core": bool(core_hits),
        "reasons": reasons,
    }


def should_escalate_to_l2(task_level: str, consensus_path: Path) -> tuple[bool, list[str]]:
    """
    编码前决策：当前任务是否需要升级为 L2 人工核准。
    :return: (是否升级, 原因列表)
    """
    if task_level == "L2":
        return False, []  # 已经是 L2，无需重复升级

    if not consensus_path.exists():
        return False, []

    text = consensus_path.read_text(encoding="utf-8")
    assessment = assess_complexity(text)

    if assessment["level"] == "L2":
        return True, assessment["reasons"]

    return False, []


def _extract_consensus_files(consensus_text: str) -> list[str]:
    """从 consensus.md 文本中提取文件路径列表"""
    files = []
    for line in consensus_text.splitlines():
        if "|" not in line or line.strip().startswith("|") and ("--" in line or "---" in line):
            continue
        cols = [c.strip() for c in line.split("|") if c.strip()]
        if not cols:
            continue
        candidate = cols[0]
        if re.search(r"(?:app|buildSrc|sport|benchmark|site-caps-ksp)/[\w./-]+\.[\w]+", candidate):
            files.append(candidate)
    return files


def record_escape(workspace: Path, escape_type: str, reason: str) -> None:
    """记录逃逸事件到 workspace/escape_log.md"""
    log_path = workspace / "escape_log.md"
    from datetime import datetime
    entry = f"## [{datetime.now().isoformat()}] {escape_type}\n\n{reason}\n\n"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        log_path.write_text(existing + entry, encoding="utf-8")
    else:
        log_path.write_text(f"# Escape Log\n\n{entry}", encoding="utf-8")
