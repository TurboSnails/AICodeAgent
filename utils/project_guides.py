#!/usr/bin/env python3
"""
项目指南加载：优先 CLAUDE.md，否则 AGENTS.md（二者可并存供 AI 阅读）。
在 planning 阶段解析构建验收策略，供 Building / AI 上下文共用。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from utils.config_loader import cfg_bool
from utils.logging_config import get_logger
from utils.paths import PROJECT_ROOT

logger = get_logger(__name__)

_GUIDE_FILES = ("CLAUDE.md", "AGENTS.md")
_GRADLEW_TASK_RE = re.compile(
    r"(?:\./)?gradlew\s+([^\n\\#]+)",
    re.IGNORECASE,
)
_TEST_TASK_MARKERS = (
    "testdebugunittest",
    "testdebug",
    ":test",
    "./gradlew test",
    "paparazzi",
    "单元测试",
    "运行测试",
)
_LINT_MARKERS = ("lintdebug", "./gradlew lint", "lint ")


@dataclass
class BuildPolicy:
    """任务级 Gradle 验收策略（由项目指南 + 需求解析）。"""

    source: str = "config"
    assemble_only: bool = True
    gradle_tasks: list[str] = field(default_factory=lambda: ["app:assembleDebug"])
    verify_command: str = "./gradlew app:assembleDebug"
    guides_excerpt: str = ""
    requirement_notes: str = ""

    def primary_task(self) -> str:
        return self.gradle_tasks[0] if self.gradle_tasks else "app:assembleDebug"


def _read_guide(path: Path, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:max_chars]
    except OSError as e:
        logger.debug("read guide %s failed: %s", path, e)
        return ""


def load_project_guides_text(*, max_chars_per_file: int = 8000) -> tuple[str, str]:
    """
    加载项目指南全文片段，供 AI 上下文使用。
    返回 (合并文本, 主来源文件名)。
    """
    parts: list[str] = []
    primary = ""
    for name in _GUIDE_FILES:
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue
        text = _read_guide(path, max_chars_per_file)
        if not text.strip():
            continue
        if not primary:
            primary = name
        parts.append(f"## {name}\n{text}")
    return ("\n\n".join(parts), primary or "config")


def append_project_guides_to_parts(parts: list[str], *, max_chars_per_file: int = 8000) -> str:
    """将项目指南追加到 context parts，返回主来源文件名。"""
    text, source = load_project_guides_text(max_chars_per_file=max_chars_per_file)
    if text.strip():
        parts.append(f"\n## 项目规范（优先 CLAUDE.md，其次 AGENTS.md）\n{text}")
    return source


def _extract_gradle_tasks_from_text(text: str) -> list[str]:
    tasks: list[str] = []
    seen: set[str] = set()
    for block in re.findall(r"```(?:bash|sh|shell)?\s*([\s\S]*?)```", text, re.I):
        for m in _GRADLEW_TASK_RE.finditer(block):
            raw = m.group(1).strip()
            for token in raw.split():
                t = token.strip().strip('"').strip("'")
                if not t or t.startswith("-") or t in seen:
                    continue
                if ":" in t or t in ("clean", "test", "lint"):
                    seen.add(t)
                    tasks.append(t)
    return tasks


def _requirement_wants_tests(requirement: str) -> bool:
    req = (requirement or "").lower()
    return any(m in req for m in _TEST_TASK_MARKERS)


def _requirement_wants_lint(requirement: str) -> bool:
    req = (requirement or "").lower()
    return any(m in req for m in _LINT_MARKERS)


def resolve_build_policy(
    requirement: str = "",
    *,
    level: str = "",
) -> BuildPolicy:
    """
    根据 CLAUDE.md / AGENTS.md 与需求文本决定 Gradle 验收步骤。
    默认与仓库文档一致：Agent 编译验收 = app:assembleDebug。
    """
    guides_text, guide_source = load_project_guides_text(max_chars_per_file=6000)
    guide_tasks = _extract_gradle_tasks_from_text(guides_text)

    verify_task = "app:assembleDebug"
    for preferred in ("app:assembleDebug", "assembleDebug"):
        if preferred in guide_tasks:
            verify_task = preferred
            break
    else:
        for t in guide_tasks:
            if "assemble" in t.lower():
                verify_task = t
                break

    wants_tests = _requirement_wants_tests(requirement)
    wants_lint = _requirement_wants_lint(requirement)

    assemble_only = cfg_bool("build.assemble_only", True) or (
        level == "L0" and cfg_bool("build.l0_assemble_only", True)
    )
    if wants_tests or wants_lint:
        assemble_only = False

    if assemble_only:
        gradle_tasks = [verify_task]
    else:
        gradle_tasks = [verify_task]
        if wants_tests:
            if "testDebugUnitTest" in guide_tasks:
                gradle_tasks.append("testDebugUnitTest")
            elif "test" in guide_tasks:
                gradle_tasks.append("test")
            else:
                gradle_tasks.append("testDebugUnitTest")
        if wants_lint:
            if "lintDebug" in guide_tasks:
                gradle_tasks.append("lintDebug")
            elif "lint" in guide_tasks:
                gradle_tasks.append("lint")
            else:
                gradle_tasks.append("lintDebug")

    source = guide_source if guide_source else "config"
    if wants_tests or wants_lint:
        source = f"{source}+requirement"

    policy = BuildPolicy(
        source=source,
        assemble_only=assemble_only,
        gradle_tasks=gradle_tasks,
        verify_command=f"./gradlew {verify_task}",
        guides_excerpt=guides_text[:4000],
        requirement_notes=(
            "需求要求跑测试" if wants_tests else "需求未要求测试；按项目指南仅编译验收"
        ),
    )
    logger.info(
        "Build policy: assemble_only=%s tasks=%s source=%s",
        policy.assemble_only,
        policy.gradle_tasks,
        policy.source,
    )
    return policy


def write_build_policy_files(workspace: Path, policy: BuildPolicy) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "build_policy.json").write_text(
        json.dumps(asdict(policy), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = [
        "# Build Policy\n",
        f"- **来源**: {policy.source}\n",
        f"- **验收命令**: `{policy.verify_command}`\n",
        f"- **仅编译 (assemble_only)**: {policy.assemble_only}\n",
        f"- **Gradle 任务序列**: `{', '.join(policy.gradle_tasks)}`\n",
        f"- **说明**: {policy.requirement_notes}\n",
    ]
    if policy.guides_excerpt:
        md.append("\n## 项目指南摘要\n")
        md.append(policy.guides_excerpt[:3000])
    (workspace / "build_policy.md").write_text("".join(md), encoding="utf-8")

    guides_full, _ = load_project_guides_text(max_chars_per_file=12000)
    if guides_full.strip():
        (workspace / "project_guides.md").write_text(guides_full, encoding="utf-8")


def read_build_policy(workspace: Path) -> Optional[BuildPolicy]:
    path = workspace / "build_policy.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BuildPolicy(
            source=data.get("source", "workspace"),
            assemble_only=bool(data.get("assemble_only", True)),
            gradle_tasks=list(data.get("gradle_tasks") or ["app:assembleDebug"]),
            verify_command=data.get("verify_command", "./gradlew app:assembleDebug"),
            guides_excerpt=data.get("guides_excerpt", ""),
            requirement_notes=data.get("requirement_notes", ""),
        )
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning("read build_policy.json failed: %s", e)
        return None
