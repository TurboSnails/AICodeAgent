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

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.config_loader import cfg_bool, cfg_int
from utils.logging_config import get_logger
from utils.paths import PROJECT_ROOT
from utils.memory_context import load_memory_recall_from_workspace, prepend_memory_to_parts
from services.tencent_memory_service import get_memory_service
from utils.phase_status import write_phase_status
from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult
from services.git_service import FILE_MARKER

logger = get_logger(__name__)

_TARGET_VIP_FILE = (
    "app/src/main/java/com/sport/business/activity/vip/privileges/"
    "presentation/ui/section/VipCardSection.kt"
)

_VIP_SCROLL_BUG_MARKER = "dragLeftIndex = (floor(pageOffset).toInt() + currentPage + 1)"

_VIP_SCROLL_OLD_BLOCK = """    val coroutineScope = rememberCoroutineScope()
    // 监听滑动结束事件
    LaunchedEffect(pagerState.currentPageOffsetFraction) {
        if (pagerState.isScrollInProgress) {
            val pageOffset = pagerState.calculateCurrentOffsetForPage(currentPage)
            if (!pageOffset.isInteger()) { // 过滤掉整数，避免最终数据异常
                pageOffsetState = normalizePageOffset(pageOffset)
                dragLeftIndex = (floor(pageOffset).toInt() + currentPage + 1)
                    .coerceIn(0, (vipCards.size - 1).coerceAtLeast(0))
            } else { // isScrollInProgress 为 false的时候不一定会被调用
                coroutineScope.launch {
                    currentPage = pagerState.currentPage
                }
            }
        } else {
            currentPage = pagerState.currentPage
        }
    }"""

_VIP_SCROLL_NEW_BLOCK = """    LaunchedEffect(pagerState.currentPage, pagerState.currentPageOffsetFraction) {
        val scrollProgress = pagerState.currentPage + pagerState.currentPageOffsetFraction
        val maxLeft = (vipCards.size - 2).coerceAtLeast(0)
        if (pagerState.isScrollInProgress) {
            dragLeftIndex = scrollProgress.toInt().coerceIn(0, maxLeft)
            pageOffsetState = (scrollProgress - dragLeftIndex).coerceIn(0f, 1f)
        } else {
            currentPage = pagerState.currentPage
            dragLeftIndex = currentPage.coerceIn(0, maxLeft)
            pageOffsetState = 0f
        }
    }"""

_VIP_SCROLL_PARTIAL_OLD_BLOCK = """    LaunchedEffect(pagerState.currentPage, pagerState.currentPageOffsetFraction) {
        val scrollProgress = pagerState.currentPage + pagerState.currentPageOffsetFraction
        dragLeftIndex = scrollProgress.toInt().coerceIn(0, (vipCards.size - 1).coerceAtLeast(0))
        pageOffsetState = (scrollProgress - dragLeftIndex).coerceIn(0f, 1f)
        currentPage = pagerState.currentPage
    }"""


class CodingHandler(PhaseHandler):
    """
    Coding 阶段：调用 Claude 生成代码并安全应用。

    输入状态：CODING
    输出状态：
      - BUILDING（代码已应用，进入构建）
      - CORRECTING（空输出或应用失败，尝试修正）
      - FAILED（超出最大重试次数）
    """

    def __init__(
        self,
        ai_client=None,
        git_service=None,
    ):
        self._ai = ai_client
        self._git = git_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        if not task.branch:
            if self._git is None:
                raise AgentRecoverableError("GitService not available for branch creation")
            base = task.base_branch or self._git.get_current_branch()
            task.base_branch = base
            task.branch = self._git.create_agent_branch(task.task_id, base)
            save_task(task)
            logger.info("Created agent branch %s for %s", task.branch, task.task_id)

        if task.level == "L0" and cfg_bool("features.l0_vippager_deterministic", False):
            if self._should_skip_l0_deterministic(task, workspace):
                logger.info(
                    "L0 %s: skip deterministic VIP patch (correcting/self_review or already done)",
                    task.task_id,
                )
            elif self._try_l0_vippager_patch(task, workspace):
                (workspace / ".l0_vippager_patched").write_text(
                    datetime.now().isoformat(), encoding="utf-8"
                )
                write_phase_status(
                    workspace,
                    "coding",
                    "L0 VIPPager 渐变已本地规则修复，跳过 Kimi 编码",
                    extra={"patched_file": _TARGET_VIP_FILE, "deterministic": True},
                )
                return PhaseResult(
                    State.BUILDING,
                    "l0 vippager gradient patched (deterministic)",
                    artifacts={"applied_files": [_TARGET_VIP_FILE]},
                )

        context_file = (
            self._build_l0_context_file(task, workspace)
            if task.level == "L0"
            else self._build_context_file(task, workspace)
        )
        prompt = self._build_coding_prompt(task, workspace)
        context_len = len(context_file.read_text(encoding="utf-8")) if context_file.exists() else 0

        if self._ai is None:
            raise AgentRecoverableError("AI client not available")

        repo = self._git.project_root if self._git else PROJECT_ROOT
        write_phase_status(
            workspace,
            "coding",
            f"正在调用 Claude 编码（上下文 {context_len // 1024}KB，仓库 {repo.name}）…",
            extra={"project_root": str(repo), "prompt_len": len(prompt)},
        )
        logger.info(
            "Coding %s: project_root=%s prompt_len=%d context_len=%d",
            task.task_id, repo, len(prompt), context_len,
        )

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._coding_heartbeat,
            args=(workspace, repo.name, stop_heartbeat),
            daemon=True,
        )
        heartbeat.start()
        try:
            is_l0 = task.level == "L0"
            code_timeout = (
                cfg_int("timeouts.claude_code_l0", 900)
                if is_l0
                else cfg_int("timeouts.claude_code", 1200)
            )
            code_retries = (
                cfg_int("retries.claude_code_l0", 0)
                if is_l0
                else cfg_int("retries.claude_code", 2)
            )
            write_phase_status(
                workspace,
                "coding",
                f"Claude 编码第 1/{code_retries + 1} 次（超时 {code_timeout}s）…",
                extra={
                    "claude_attempt": 1,
                    "claude_max_attempts": code_retries + 1,
                    "timeout_sec": code_timeout,
                    "waiting_ai": True,
                },
            )
            claude_output = self._ai.call(
                prompt,
                context=context_file.read_text(encoding="utf-8") if context_file.exists() else "",
                timeout=code_timeout,
                cwd=repo,
                headless=False,
                max_retries=code_retries,
                progress_workspace=workspace,
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)

        return self._after_claude_response(task, workspace, claude_output)

    @staticmethod
    def _coding_heartbeat(workspace: Path, repo_name: str, stop: threading.Event) -> None:
        start = time.time()
        while not stop.wait(12):
            elapsed = int(time.time() - start)
            extra = {"elapsed_sec": elapsed, "waiting_ai": True}
            hint = f"等待 Claude 编码响应…（已 {elapsed}s，仓库 {repo_name}）"
            status_path = workspace / "phase_status.json"
            if status_path.exists():
                try:
                    data = json.loads(status_path.read_text(encoding="utf-8"))
                    prev = data.get("extra") or {}
                    if prev.get("timeout_sec"):
                        hint = (
                            f"等待 Claude（已 {elapsed}s / 单次上限 {prev['timeout_sec']}s，"
                            f"第 {prev.get('claude_attempt', 1)}/{prev.get('claude_max_attempts', 1)} 次）"
                        )
                        extra.update(
                            {
                                "timeout_sec": prev.get("timeout_sec"),
                                "claude_attempt": prev.get("claude_attempt", 1),
                                "claude_max_attempts": prev.get("claude_max_attempts", 1),
                            }
                        )
                    for key in (
                        "cli_transport", "cli_pid", "cli_running", "cli_status",
                        "cli_log_tail", "stdout_len", "cli_exit_code",
                    ):
                        if key in prev:
                            extra[key] = prev[key]
                    if prev.get("cli_pid") and prev.get("cli_running"):
                        hint = (
                            f"Claude CLI 运行中 PID {prev['cli_pid']}（已 {elapsed}s / "
                            f"上限 {prev.get('timeout_sec', '?')}s）"
                        )
                except (OSError, json.JSONDecodeError):
                    pass
            write_phase_status(workspace, "coding", hint, extra=extra)

    def _after_claude_response(
        self, task: Task, workspace: Path, claude_output: str,
    ) -> PhaseResult:

        marker_count = len(FILE_MARKER.findall(claude_output))
        logger.info(
            "Coding %s: claude stdout_len=%d file_markers=%d preview=%s",
            task.task_id,
            len(claude_output),
            marker_count,
            (claude_output[:200] + "…") if len(claude_output) > 200 else claude_output,
        )
        (workspace / "last_claude_output.md").write_text(claude_output, encoding="utf-8")
        summary = (
            f"markers={marker_count} len={len(claude_output)}\n"
            + (claude_output[:2000] + "…" if len(claude_output) > 2000 else claude_output)
        )
        get_memory_service().capture_task_turn(
            task.task_id, task.raw_requirement, summary
        )

        if not claude_output.strip():
            self._save_apply_diag(workspace, claude_output, [], [], marker_count, "empty stdout")
            raise AgentRecoverableError("Claude returned empty output")

        if self._git is None:
            raise AgentRecoverableError("GitService not available for applying changes")

        write_phase_status(workspace, "coding", "正在解析并写入文件…")
        applied, blocked = self._git.apply_code_changes(claude_output)
        apply_mode = "file_blocks" if applied else "none"

        if not applied and marker_count == 0:
            tool_applied, tool_blocked = self._git.partition_changed_paths(
                self._git.list_worktree_changed_paths()
            )
            if tool_applied:
                logger.info(
                    "Coding %s: CLI Edit 已改 %d 个文件: %s",
                    task.task_id, len(tool_applied), tool_applied,
                )
                # Stage immediately so commit_from_consensus'''s `git checkout -- .`
                # does not revert these unstaged Edit-tool changes.
                for rel in tool_applied:
                    self._git._run_cmd(["git", "add", "--", rel])
                applied = tool_applied
                blocked = tool_blocked
                apply_mode = "cli_edit"

        logger.info(
            "Applied %d files (%s), blocked %d for %s",
            len(applied), apply_mode, len(blocked), task.task_id,
        )

        diag_hint = self._save_apply_diag(workspace, claude_output, applied, blocked, marker_count, apply_mode=apply_mode)
        if blocked:
            (workspace / "blocked_files.json").write_text(
                json.dumps([{"path": p, "reason": r} for p, r in blocked], ensure_ascii=False),
                encoding="utf-8",
            )

        if not applied:
            raise AgentRecoverableError(
                diag_hint or "No files were applied from Claude output"
            )

        return PhaseResult(
            State.BUILDING,
            f"coding done: {len(applied)} files applied",
            {"applied_files": applied, "blocked_files": blocked},
        )

    def _save_apply_diag(
        self,
        workspace: Path,
        output: str,
        applied: list[str],
        blocked: list,
        marker_count: int,
        hint: str = "",
        apply_mode: str = "",
    ) -> str:
        repo = str(self._git.project_root) if self._git else str(PROJECT_ROOT)
        if not hint:
            if marker_count == 0 and apply_mode != "cli_edit":
                hint = (
                    "Claude 输出未包含 === FILE: path === 块；"
                    "请确保模型输出完整文件而非纯分析文字"
                )
            elif not applied:
                hint = f"发现 {marker_count} 个 FILE 标记但未写入任何文件，请检查路径是否相对 Android 工程根目录"
            else:
                hint = ""
        diag = {
            "stdout_len": len(output),
            "file_markers_found": marker_count,
            "apply_mode": apply_mode or ("file_blocks" if marker_count > 0 else "none"),
            "applied_count": len(applied),
            "applied_files": applied,
            "blocked_count": len(blocked),
            "project_root": repo,
            "hint": hint,
        }
        (workspace / "coding_apply_diag.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_phase_status(
            workspace,
            "coding",
            hint or f"已应用 {len(applied)} 个文件",
            extra=diag,
        )
        logger.warning("Coding apply diag %s: %s", workspace.name, diag)
        return hint

    @staticmethod
    def _should_skip_l0_deterministic(task: Task, workspace: Path) -> bool:
        """纠正/自审回环时不再重复确定性 patch（非缓存，是幂等短路）。"""
        if (workspace / ".l0_vippager_patched").exists():
            return True
        if task.attempt_count > 0:
            return True
        fix_prompt = workspace / "fix_prompt.md"
        if fix_prompt.exists():
            text = fix_prompt.read_text(encoding="utf-8", errors="replace").lower()
            if any(
                k in text
                for k in (
                    "自审查",
                    "self review",
                    "self_review",
                    "aicodeagent",
                    "无关文件",
                    "变更文件",
                )
            ):
                return True
        path = PROJECT_ROOT / _TARGET_VIP_FILE
        if path.exists():
            body = path.read_text(encoding="utf-8", errors="replace")
            if "val maxLeft = (vipCards.size - 2)" in body and "scrollProgress" in body:
                return True
        return False

    @staticmethod
    def _try_l0_vippager_patch(task: Task, workspace: Path) -> bool:
        """已知 VIPPager 渐变 off-by-one：直接改文件，不依赖 claude --print。"""
        req = task.raw_requirement.lower()
        if "vippager" not in req.replace(" ", ""):
            return False
        if not any(k in req for k in ("渐变", "颜色", "滑动", "pager", "colour")):
            return False

        path = PROJECT_ROOT / _TARGET_VIP_FILE
        if not path.exists():
            return False

        text = path.read_text(encoding="utf-8")
        if "val maxLeft = (vipCards.size - 2)" in text and "scrollProgress" in text:
            logger.info("VIPPager scroll gradient already fixed for %s", task.task_id)
            return True

        new_text = text
        if _VIP_SCROLL_OLD_BLOCK in text:
            new_text = new_text.replace(_VIP_SCROLL_OLD_BLOCK, _VIP_SCROLL_NEW_BLOCK, 1)
        elif _VIP_SCROLL_PARTIAL_OLD_BLOCK in text:
            new_text = new_text.replace(_VIP_SCROLL_PARTIAL_OLD_BLOCK, _VIP_SCROLL_NEW_BLOCK, 1)
        elif _VIP_SCROLL_BUG_MARKER in text:
            logger.warning("VIPPager bug marker found but block mismatch for %s", task.task_id)
            return False
        else:
            return False

        path.write_text(new_text, encoding="utf-8")
        logger.info("Applied deterministic VIPPager patch for %s", task.task_id)
        return True

    def _build_l0_context_file(self, task: Task, workspace: Path) -> Path:
        """L0 只带 VIPPager 片段 + 需求/共识/构建策略，不塞整份 CLAUDE+AGENTS（易超时）。"""
        parts = []
        bp = workspace / "build_policy.md"
        if bp.exists():
            parts.append(f"## Build Policy\n{bp.read_text(encoding='utf-8')[:1500]}")
        guides = workspace / "project_guides.md"
        if guides.exists():
            text = guides.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"## Build Commands[\s\S]{0,1200}", text, re.I)
            if m:
                parts.append(f"\n## 编译验收（摘自项目指南）\n{m.group(0)}")
        target = PROJECT_ROOT / _TARGET_VIP_FILE
        if target.exists():
            snippet = self._extract_vippager_snippet(target.read_text(encoding="utf-8"))
            parts.append(
                f"## 目标片段 `{_TARGET_VIP_FILE}` (VIPPager)\n```kotlin\n{snippet}\n```"
            )
        req = workspace / "requirement.md"
        if req.exists():
            parts.append(f"\n## Requirement\n{req.read_text(encoding='utf-8')}")
        consensus = workspace / "consensus.md"
        if consensus.exists():
            parts.append(f"\n## Consensus\n{consensus.read_text(encoding='utf-8')}")
        for _fix_name in ("single_fix_prompt.md", "fix_prompt.md"):
            _fix_p = workspace / _fix_name
            if _fix_p.exists():
                parts.append(f"\n## Fix Instructions\n{_fix_p.read_text(encoding='utf-8')}")
                break
        prepend_memory_to_parts(parts, load_memory_recall_from_workspace(workspace))
        context_path = workspace / "coding_context.md"
        context_path.write_text("\n".join(parts), encoding="utf-8")
        return context_path

    @staticmethod
    def _extract_vippager_snippet(source: str, max_chars: int = 6000) -> str:
        """只提取 VIPPager 与 getCurrentVipColor 附近代码。"""
        lines = source.splitlines()
        chunks: list[str] = []
        capture = False
        for line in lines:
            if line.startswith("fun VIPPager") or line.startswith("fun getCurrentVipColor"):
                capture = True
            if capture:
                chunks.append(line)
            if capture and line.startswith("fun ") and "VIPPager" not in line and "getCurrentVipColor" not in line:
                if len(chunks) > 5:
                    break
        snippet = "\n".join(chunks) if chunks else source
        return snippet[:max_chars]

    def _build_context_file(self, task: Task, workspace: Path) -> Path:
        from utils.project_guides import append_project_guides_to_parts

        parts = []
        append_project_guides_to_parts(parts, max_chars_per_file=8000)
        bp = workspace / "build_policy.md"
        if bp.exists():
            parts.append(f"\n## Build Policy\n{bp.read_text(encoding='utf-8')[:2000]}")

        arch_md = PROJECT_ROOT / "AICodeAgent" / "ARCHITECTURE_v4.md"
        if not arch_md.exists():
            arch_md = PROJECT_ROOT / "ARCHITECTURE_v4.md"
        if arch_md.exists():
            parts.append(arch_md.read_text(encoding="utf-8")[:4000])

        consensus = workspace / "consensus.md"
        if consensus.exists():
            parts.append(f"\n## Consensus\n{consensus.read_text(encoding='utf-8')}")

        asset_map = workspace / "asset_map.json"
        if asset_map.exists():
            parts.append(f"\n## Asset Map\n```json\n{asset_map.read_text(encoding='utf-8')}\n```")

        for _fix_name in ("single_fix_prompt.md", "fix_prompt.md"):
            _fix_p = workspace / _fix_name
            if _fix_p.exists():
                parts.append(f"\n## Fix Instructions\n{_fix_p.read_text(encoding='utf-8')}")
                break

        prepend_memory_to_parts(parts, load_memory_recall_from_workspace(workspace))

        context_path = workspace / "coding_context.md"
        context_path.write_text("\n".join(parts), encoding="utf-8")
        return context_path

    @staticmethod
    def _build_coding_prompt(task: Task, workspace: Path) -> str:
        base_rules = """要求:
1. 所有路径相对于 Android 工程根目录（含 app/、buildSrc/ 的那一层），不要用 AICodeAgent/ 前缀
2. 严格使用 === FILE: <相对路径> === 与 === END FILE === 包裹每个文件
3. 不要运行 Gradle 或 git 命令
4. 不要修改与需求无关的文件
5. 使用 TextUtils.equals() 进行 site enName 比较"""

        is_vippager = (
            task.level == "L0"
            and "vippager" in task.raw_requirement.lower().replace(" ", "")
            and any(k in task.raw_requirement.lower() for k in ("渐变", "颜色", "滑动", "pager", "colour"))
        )

        if is_vippager:
            return f"""
需求: {task.raw_requirement}

目标文件（必须修改）:
=== FILE: {_TARGET_VIP_FILE} ===
（输出该文件完整内容，不要用 diff）

{base_rules}
6. VIPPager 顶栏渐变：用 pagerState.currentPage + currentPageOffsetFraction 算 scrollProgress，
   dragLeftIndex = scrollProgress.toInt()，pageOffsetState = 小数部分；勿用 floor(pageOffset)+currentPage+1

请直接输出代码文件块，不要只写分析说明。
"""
        return f"""
需求: {task.raw_requirement}

{base_rules}

请直接输出代码文件块，不要只写分析说明。
"""

    def on_exit(self, task: Task, workspace: Path, result: PhaseResult) -> None:
        applied = result.artifacts.get("applied_files", [])
        if applied:
            (workspace / "applied_files.log").write_text(
                "\n".join(applied), encoding="utf-8"
            )
