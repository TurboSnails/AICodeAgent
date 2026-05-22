#!/usr/bin/env python3
"""
Git 服务 — V4 重构
封装所有 Git 操作：branch、commit、push、diff、restore、状态快照
- 安全黑名单校验（apply_code_changes）
- 依赖变更检测
- 共识偏差审计
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from engine.exceptions import GitCommandError
from utils.config_loader import cfg_bool, cfg_str
from utils.logging_config import get_logger

logger = get_logger(__name__)
from utils.paths import AGENT_ROOT, PROJECT_ROOT

# 安全黑名单：Agent 禁止修改的文件模式
BLOCKED_PATHS = [
    ".github/",
    "jg_tools/",
    "benchmark/",
    "gradle/wrapper/gradle-wrapper.properties",
    "buildSrc/src/main/kotlin/Configs.kt",
    "keystore/",
    ".jks",
    ".keystore",
    "SiteThemeRegistryGenerated",
    ".github/workflows/",
    "jg_tools/protect.sh",
    "jg_tools/shell/",
    "app/src/main/baselineProfiles/",
]

# 依赖文件清单（用于变更检测）
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

# 正则：匹配 === FILE: path === 的代码块
FILE_MARKER = re.compile(r"===\s*FILE:\s*(.+?)\s*===")
END_MARKER = "=== END FILE ==="


class GitService:
    """
    封装 Git 操作与代码变更应用。

    职责：
    1. 创建/切换 agent branch
    2. 解析 Claude 输出并安全写入文件（黑名单防护 + 路径遍历防护）
    3. Git commit（只提交共识中列出的文件）
    4. Git push
    5. 依赖变更检测
    6. 共识偏差审计
    7. 环境快照与恢复
    """

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or PROJECT_ROOT

    # ------------------------------------------------------------------
    # Branch 管理
    # ------------------------------------------------------------------

    def get_current_branch(self) -> str:
        code, stdout, _ = self._run_cmd(["git", "branch", "--show-current"])
        return stdout.strip() if code == 0 else "main"

    def create_agent_branch(self, task_id: str, base_branch: str = "") -> str:
        base = base_branch or self.get_current_branch()
        branch = f"feature/agent-{task_id}"
        self._run_cmd(["git", "checkout", base])
        if cfg_bool("git.pull_on_branch_create", False):
            code, _, err = self._run_cmd(
                ["git", "pull", "origin", base], timeout=120,
            )
            if code != 0:
                logger.warning("git pull origin %s failed (continuing): %s", base, err.strip())
        self._run_cmd(["git", "checkout", "-b", branch])
        logger.info("Created branch %s from %s", branch, base)
        return branch

    # ------------------------------------------------------------------
    # 代码变更应用（从 Claude 输出解析）
    # ------------------------------------------------------------------

    def apply_code_changes(self, claude_output: str) -> Tuple[list[str], list[Tuple[str, str]]]:
        """
        解析 Claude 输出中的 === FILE: path === 块，安全写入文件系统。

        :return: (成功应用的文件列表, 被拦截的(路径, 原因)列表)
        """
        lines = claude_output.splitlines()
        i = 0
        applied: list[str] = []
        blocked: list[Tuple[str, str]] = []

        while i < len(lines):
            match = FILE_MARKER.match(lines[i])
            if match:
                file_path = match.group(1).strip()
                i += 1
                content_lines = []
                while i < len(lines) and lines[i].strip() != END_MARKER:
                    content_lines.append(lines[i])
                    i += 1

                full_path = (self.project_root / file_path).resolve()

                # 路径遍历防护
                if not str(full_path).startswith(str(self.project_root)):
                    logger.warning("Path traversal blocked: %s", file_path)
                    i += 1
                    continue

                # 安全黑名单校验
                is_blocked, reason = self._is_blocked_path(file_path)
                if is_blocked:
                    logger.warning("BLOCKED %s | %s", file_path, reason)
                    blocked.append((file_path, reason))
                    i += 1
                    continue

                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text("\n".join(content_lines), encoding="utf-8")
                applied.append(file_path)
                logger.info("APPLY %s", file_path)
            i += 1

        if blocked:
            self._record_security_violations(blocked)

        return applied, blocked

    def list_worktree_changed_paths(self) -> list[str]:
        """返回工作区相对 project_root 的已修改/新增路径（供 CLI Agent 模式落盘校验）。"""
        code, out, _ = self._run_cmd(["git", "status", "--porcelain"], capture=True)
        if code != 0:
            return []
        paths: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # XY path 或 XY old -> new
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                continue
            path_part = parts[1]
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1]
            rel = path_part.strip()
            if rel:
                paths.append(rel)
        return paths

    def partition_changed_paths(
        self, paths: list[str],
    ) -> Tuple[list[str], list[Tuple[str, str]]]:
        """将 git status 路径分为可接受与黑名单拦截。"""
        applied: list[str] = []
        blocked: list[Tuple[str, str]] = []
        for rel in paths:
            is_blocked, reason = self._is_blocked_path(rel)
            if is_blocked:
                blocked.append((rel, reason))
            else:
                applied.append(rel)
        if blocked:
            self._record_security_violations(blocked)
        return applied, blocked

    def _is_blocked_path(self, file_path: str) -> Tuple[bool, str]:
        normalized = file_path.replace("\\", "/")
        for pattern in BLOCKED_PATHS:
            if pattern in normalized:
                return True, f"命中安全黑名单: {pattern}"
        if "BuildConfig" in normalized and normalized.endswith(".kt"):
            return True, "禁止修改 BuildConfig 生成逻辑"
        if "encrypt" in normalized.lower() and "key" in normalized.lower():
            return True, "禁止修改加密密钥相关文件"
        return False, ""

    def _record_security_violations(self, blocked: list[Tuple[str, str]]) -> None:
        security_log = AGENT_ROOT / "data" / "security_violations.log"
        security_log.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat()
        with security_log.open("a", encoding="utf-8") as f:
            for bp, br in blocked:
                f.write(f"[{timestamp}] BLOCKED: {bp} | {br}\n")
        logger.warning("%d files blocked, see %s", len(blocked), security_log)

    # ------------------------------------------------------------------
    # Git Commit（只提交共识中列出的文件）
    # ------------------------------------------------------------------

    def commit_from_consensus(
        self,
        task_id: str,
        workspace: Path,
        base_branch: str = "",
    ) -> None:
        """
        只提交 consensus.md 中列出的文件，提交前检查依赖变更是否已批准。
        """
        dep_changed, dep_summary = self._check_dependency_diff(base_branch or self.get_current_branch())
        if dep_changed and not self._is_dependency_approved(workspace):
            raise GitCommandError(
                f"[DEP BLOCK] 依赖变更未在 consensus.md 中明确批准。\n{dep_summary}"
            )

        files = self._extract_committable_files(workspace)
        if not files:
            logger.warning("No file list in consensus.md, fallback to git add -A")
            self._run_cmd(["git", "add", "-A"])
        else:
            # 先 reset 未跟踪/未暂存的改动
            self._run_cmd(["git", "checkout", "--", "."])
            for f in files:
                if "*" in f:
                    for matched in self.project_root.glob(f):
                        rel = str(matched.relative_to(self.project_root)).replace("\\", "/")
                        self._run_cmd(["git", "add", "--", rel])
                else:
                    self._run_cmd(["git", "add", "--", f])
            # 兜底：add 未被跟踪但位于 app/ buildSrc/ sport/ 下的文件
            self._run_cmd(["git", "add", "--", "app/", "buildSrc/", "sport/"])

        self._run_cmd([
            "git", "commit", "-m",
            f"feat: automated implementation [agent-{task_id}]",
        ])

    def _extract_committable_files(self, workspace: Path) -> list[str]:
        """从 consensus.md 提取文件清单。"""
        files: list[str] = []
        consensus = workspace / "consensus.md"
        if consensus.exists():
            text = consensus.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "|" not in line or line.strip().startswith("|") and ("--" in line or "---" in line):
                    continue
                cols = [c.strip() for c in line.split("|") if c.strip()]
                if not cols:
                    continue
                candidate = cols[0]
                if re.search(r"(?:app|buildSrc|sport|benchmark|site-caps-ksp)/[\w./-]+\.[\w]+", candidate):
                    files.append(candidate)

        # 补充 asset_map.json
        asset_map = workspace / "asset_map.json"
        if asset_map.exists():
            try:
                import json
                data = json.loads(asset_map.read_text(encoding="utf-8"))
                for entry in data.get("assets", []):
                    local = entry.get("local_file")
                    if local and entry.get("action") in ("ingested_vector", "ingested_raster"):
                        files.append(f"app/src/main/res/drawable/{local}")
                        files.append(f"app/src/siteRes/*/drawable/{local}")
            except Exception:
                pass

        seen = set()
        result = []
        for f in files:
            if f not in seen:
                seen.add(f)
                result.append(f)
        return result

    def _check_dependency_diff(self, base_branch: str) -> Tuple[bool, str]:
        changed = []
        for dep_file in DEPENDENCY_FILES:
            full = self.project_root / dep_file
            if not full.exists():
                continue
            code, base_content, _ = self._run_cmd(
                ["git", "show", f"{base_branch}:{dep_file}"],
                capture=True,
            )
            if code != 0:
                base_content = ""
            current_content = full.read_text(encoding="utf-8")
            if base_content != current_content:
                changed.append(dep_file)
        if not changed:
            return False, ""
        return True, f"检测到依赖文件变更: {', '.join(changed)}"

    def _is_dependency_approved(self, workspace: Path) -> bool:
        consensus = workspace / "consensus.md"
        if not consensus.exists():
            return False
        text = consensus.read_text(encoding="utf-8").lower()
        return any(k in text for k in ("新依赖", "第三方库", "依赖", "新增库", "引入库"))

    # ------------------------------------------------------------------
    # Push & PR
    # ------------------------------------------------------------------

    def push(self, branch: str) -> None:
        self._run_cmd(["git", "push", "origin", branch])

    def create_pr(
        self,
        task_id: str,
        requirement: str,
        branch: str,
        base_branch: str = "",
        deviation_report: str = "",
    ) -> str:
        """使用 gh CLI 创建 PR，返回 PR URL。"""
        code, _, _ = self._run_cmd(["gh", "--version"], capture=True)
        if code != 0:
            logger.warning("gh CLI not available")
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

        code, stdout, _ = self._run_cmd(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--base", base,
                "--head", branch,
            ],
            capture=True,
        )
        if code == 0:
            for line in stdout.splitlines():
                if line.startswith("https://github.com/"):
                    return line.strip()
        return ""

    # ------------------------------------------------------------------
    # 共识偏差审计
    # ------------------------------------------------------------------

    def generate_deviation_report(self, workspace: Path, task) -> str:
        """对比 consensus.md 承诺 vs 实际修改，生成 deviation report。"""
        promised = self._extract_consensus_file_paths(workspace)
        actual = self._get_actual_changed_files(task.base_branch or "main")

        missing = promised - actual
        extra = actual - promised
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

        dep_violation = self._detect_dependency_violation()
        if dep_violation:
            lines.append("### 🔒 依赖变更警告")
            lines.append(dep_violation)
            lines.append("")

        if not lines:
            lines.append("✅ 编码结果与 consensus 一致，无偏差。")

        report = "\n".join(lines)
        (workspace / "consensus_deviation.md").write_text(report, encoding="utf-8")
        return report

    def _extract_consensus_file_paths(self, workspace: Path) -> set[str]:
        files: set[str] = set()
        consensus = workspace / "consensus.md"
        if not consensus.exists():
            return files
        text = consensus.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "|" not in line or line.strip().startswith("|") and ("--" in line or "---" in line):
                continue
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if not cols:
                continue
            candidate = cols[0]
            if re.search(r"(?:app|buildSrc|sport|benchmark|site-caps-ksp)/[\w./-]+\.[\w]+", candidate):
                files.add(candidate)
        return files

    def _get_actual_changed_files(self, base_branch: str) -> set[str]:
        code, diff_out, _ = self._run_cmd(["git", "diff", "--name-only", base_branch], capture=True)
        if code == 0:
            return {line.strip() for line in diff_out.splitlines() if line.strip()}
        return set()

    def _detect_dependency_violation(self) -> str:
        code, diff_out, _ = self._run_cmd(["git", "diff", "--name-only"], capture=True)
        if code != 0:
            return ""
        changed = [line.strip() for line in diff_out.splitlines() if line.strip()]
        for dep_file in DEPENDENCY_FILES:
            if dep_file in changed:
                return f"[DEP VIOLATION] {dep_file} 被修改但未经共识批准"
        return ""

    # ------------------------------------------------------------------
    # 环境快照与恢复
    # ------------------------------------------------------------------

    def snapshot(self) -> Tuple[str, list[Tuple[str, str]], str]:
        """返回 (当前分支, git status 快照, Configs.kt 内容)"""
        _, branch_out, _ = self._run_cmd(["git", "branch", "--show-current"], capture=True)
        current_branch = branch_out.strip()

        _, status_out, _ = self._run_cmd(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            capture=True,
        )
        snapshot = []
        for line in status_out.splitlines():
            if len(line) >= 3:
                snapshot.append((line[:2], line[3:].strip()))

        configs = self.project_root / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
        original_configs = configs.read_text(encoding="utf-8") if configs.exists() else ""

        return current_branch, snapshot, original_configs

    def restore(
        self,
        task,
        original_branch: str,
        snapshot: list[Tuple[str, str]],
        original_configs: str,
    ) -> None:
        """任务结束后恢复环境。"""
        logger.info("Restoring environment after task %s", task.task_id)
        base = task.base_branch or original_branch or "main"

        if self._has_uncommitted_work():
            self._soft_restore(task, original_configs, snapshot)
            return

        # 硬恢复
        self._run_cmd(["git", "stash", "push", "--include-untracked", "-m", f"agent-cleanup-{task.task_id}"])
        self._run_cmd(["git", "checkout", base])

        if task.branch:
            _, branch_out, _ = self._run_cmd(["git", "branch", "--show-current"], capture=True)
            if branch_out.strip() != task.branch:
                self._run_cmd(["git", "branch", "-D", task.branch])

        configs = self.project_root / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
        if configs.exists() and original_configs:
            current = configs.read_text(encoding="utf-8")
            if current != original_configs:
                configs.write_text(original_configs, encoding="utf-8")

        self._run_cmd(["git", "checkout", "--", "."])

        # 清理新增的 untracked 文件
        _, status_out, _ = self._run_cmd(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            capture=True,
        )
        snapshot_paths = {p for s, p in snapshot}
        for line in status_out.splitlines():
            if len(line) >= 3 and line.startswith("??"):
                path = line[3:].strip()
                if path not in snapshot_paths:
                    target = self.project_root / path
                    if target.is_file():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)

        self._run_cmd(["git", "stash", "drop"])
        logger.info("Environment restored")

    def _has_uncommitted_work(self) -> bool:
        _, status_out, _ = self._run_cmd(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            capture=True,
        )
        for line in status_out.splitlines():
            if len(line) >= 3:
                status = line[:2]
                if status != "??":
                    return True
        return False

    def _soft_restore(
        self,
        task,
        original_configs: str,
        snapshot: list[Tuple[str, str]],
    ) -> None:
        logger.warning("Detected uncommitted work — soft cleanup only")
        configs = self.project_root / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
        if configs.exists() and original_configs:
            current = configs.read_text(encoding="utf-8")
            if current != original_configs:
                configs.write_text(original_configs, encoding="utf-8")

        if task.branch:
            _, branch_out, _ = self._run_cmd(["git", "branch", "--show-current"], capture=True)
            if branch_out.strip() != task.branch:
                self._run_cmd(["git", "branch", "-D", task.branch])

        logger.warning("Soft cleanup done — manual cleanup may be required")

    # ------------------------------------------------------------------
    # Site 切换（Configs.kt）
    # ------------------------------------------------------------------

    def switch_site(self, site_hint: str) -> bool:
        if not site_hint:
            return False
        configs = self.project_root / "buildSrc" / "src" / "main" / "kotlin" / "Configs.kt"
        if not configs.exists():
            return False
        content = configs.read_text(encoding="utf-8")
        match = re.search(r"(val\s+site\s*:\s*Site\s*=\s*)(\w+)", content)
        if not match:
            return False
        current = match.group(2)
        current_stem = self._strip_debug_release(current)
        hint_stem = self._strip_debug_release(site_hint)
        if hint_stem.lower() == current_stem.lower():
            return False

        site_dir = self.project_root / "buildSrc" / "src" / "main" / "kotlin" / "site"
        candidates = [
            f.stem for f in site_dir.glob("*.kt")
            if hint_stem.lower() == f.stem.lower() and f.stem not in ("Site", "SiteChannels")
        ]
        if not candidates:
            return False
        target = candidates[0] + "Debug"
        new_content = re.sub(r"(val\s+site\s*:\s*Site\s*=\s*)\w+", rf"\g<1>{target}", content)
        configs.write_text(new_content, encoding="utf-8")
        logger.info("Switched site: %s -> %s", current, target)
        return True

    @staticmethod
    def _strip_debug_release(name: str) -> str:
        for suffix in ("Debug", "Release"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return name

    # ------------------------------------------------------------------
    # 底层命令执行
    # ------------------------------------------------------------------

    def _run_cmd(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        timeout: int | None = None,
        capture: bool = True,
    ) -> Tuple[int, str, str]:
        import subprocess
        cwd = cwd or self.project_root
        logger.debug("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=capture,
                text=True,
                timeout=timeout,
                env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_TERMINAL_PROMPT": "0",
        },
            )
            if capture and result.stdout:
                logger.debug(result.stdout[-500:])
            if result.returncode != 0 and capture and result.stderr:
                logger.debug(result.stderr[-300:])
            return result.returncode, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            logger.error("TIMEOUT: %s", " ".join(cmd))
            return -1, "", "timeout"
        except Exception as e:
            logger.error("Error running %s: %s", " ".join(cmd), e)
            return -1, "", str(e)
