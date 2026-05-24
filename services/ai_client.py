#!/usr/bin/env python3
"""
统一 AI 客户端 — subprocess 调用 Claude Code CLI

推荐调用方式（与 Claude Code 官方 -p / JSON 输出一致）：
  claude -p "<prompt>" --output-format json --permission-mode acceptEdits \\
    --allowed-tools Read,Edit,Glob,Grep --disallowed-tools Bash

解析 stdout JSON 的 result 字段作为模型文本；编码阶段 headless=False 时允许 Edit 直接改仓库文件。

不使用 Anthropic HTTP API、不使用 Claude Agent SDK。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Union

from engine.exceptions import (
    AgentCliUnavailableError,
    AgentContextLengthError,
    AgentEmptyOutputError,
    AgentRateLimitError,
    AgentTimeoutError,
)
from utils.config_loader import cfg_bool, cfg_float, cfg_int, cfg_str
from utils.logging_config import get_logger
from utils.tracing import is_enabled, _try_log_llm
from utils.task_cancel import raise_if_cancelled

logger = get_logger(__name__)

from utils.paths import PROJECT_ROOT


_SUPPORTED_TRANSPORT = "subprocess_cli"


class AIClient:
    """
    Claude Code CLI 封装：subprocess 调用 `claude -p` + `--output-format json`。

    职责：
    1. 构建 CLI 参数（headless 只读 vs Agent 编码）
    2. 解析 JSON 响应中的 result 字段
    3. 指数退避重试（超时除外）
    """

    def __init__(self):
        self._version_checked = False
        self._version_ok = False
        self._ensure_transport()

    @staticmethod
    def _ensure_transport() -> None:
        mode = cfg_str("ai.transport", _SUPPORTED_TRANSPORT).strip().lower()
        if mode != _SUPPORTED_TRANSPORT:
            raise AgentCliUnavailableError(
                f"ai.transport={mode!r} 不支持；当前仅支持 {_SUPPORTED_TRANSPORT!r}（subprocess 调用 claude CLI）"
            )

    # ------------------------------------------------------------------
    # 配置属性（惰性加载）
    # ------------------------------------------------------------------

    @property
    def _claude_model(self) -> str:
        return cfg_str("ai.claude_model", "")

    @property
    def _llm_timeout(self) -> int:
        return cfg_int("timeouts.llm", 500)

    @property
    def _claude_timeout(self) -> int:
        return cfg_int("timeouts.claude_code", self._llm_timeout)

    @property
    def _max_retries(self) -> int:
        return cfg_int("retries.claude_code", 2)

    @property
    def _base_delay(self) -> float:
        return cfg_float("retries.base_delay", 3.0)

    @property
    def _codex_cmd(self) -> str:
        return cfg_str("ai.codex_cmd", "").strip()

    @property
    def _codex_timeout(self) -> int:
        return cfg_int("ai.codex_timeout", 900)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def call(
        self,
        prompt: str,
        context: str = "",
        timeout: Optional[int] = None,
        cwd: Union[Path, str, None] = None,
        headless: bool = False,
        max_retries: Optional[int] = None,
        progress_workspace: Optional[Union[Path, str]] = None,
    ) -> str:
        """
        统一调用入口。

        :param prompt: 当前指令
        :param context: 项目上下文（会被拼接到 prompt 前面）
        :param timeout: 覆盖默认超时
        :param headless: True=只读/分类（Read,Glob,Grep）；False=编码 Agent（acceptEdits+Edit）
        :return: 模型输出的文本
        :raises: AgentCliUnavailableError, AgentContextLengthError,
                 AgentRateLimitError, AgentTimeoutError, AgentEmptyOutputError
        """
        self._ensure_transport()
        if not self._check_cli():
            raise AgentCliUnavailableError(
                "Claude CLI not found in PATH. Install: npm install -g @anthropic-ai/claude-code"
            )

        full_prompt = self._build_full_prompt(prompt, context)
        run_timeout = timeout if timeout is not None else self._claude_timeout

        run_cwd = Path(cwd).resolve() if cwd else None
        logger.info(
            "AIClient.call start: model=%s, prompt_len=%d, timeout=%ds, cwd=%s",
            self._claude_model or "(default)",
            len(full_prompt),
            run_timeout,
            run_cwd or "(inherit)",
        )

        last_error: Optional[Exception] = None
        start = time.time()
        model = self._claude_model or "default"
        retry_limit = self._max_retries if max_retries is None else max(0, max_retries)

        for attempt in range(retry_limit + 1):
            try:
                ws = Path(progress_workspace) if progress_workspace else None
                output = self._invoke_claude_cli_subprocess(
                    full_prompt,
                    run_timeout,
                    cwd=run_cwd,
                    headless=headless,
                    progress_workspace=ws,
                    attempt=attempt + 1,
                    max_attempts=retry_limit + 1,
                )
                if is_enabled():
                    _try_log_llm(
                        model=model,
                        prompt=full_prompt,
                        output=output,
                        duration=time.time() - start,
                        metadata={"method": "claude -p json", "attempt": attempt + 1},
                    )
                return output
            except AgentTimeoutError as e:
                logger.error(
                    "Claude call TIMEOUT (attempt %d/%d, limit=%ds): %s",
                    attempt + 1,
                    retry_limit + 1,
                    run_timeout,
                    e,
                )
                self._kill_stale_claude_cli()
                if is_enabled():
                    _try_log_llm(
                        model=model,
                        prompt=full_prompt,
                        output="",
                        duration=time.time() - start,
                        error=str(e),
                        metadata={"method": "claude -p json", "attempt": attempt + 1},
                    )
                raise
            except (AgentRateLimitError, AgentEmptyOutputError) as e:
                last_error = e
                if attempt < retry_limit:
                    delay = (2 ** attempt) * self._base_delay
                    logger.info(
                        "Retrying in %.1fs... (attempt %d/%d, %s)",
                        delay,
                        attempt + 1,
                        retry_limit + 1,
                        e,
                    )
                    time.sleep(delay)
                else:
                    if is_enabled():
                        _try_log_llm(
                            model=model,
                            prompt=full_prompt,
                            output="",
                            duration=time.time() - start,
                            error=str(e),
                            metadata={"method": "claude -p json"},
                        )
                    raise

        # 理论上不会到达这里，因为最后一次重试会 raise
        raise last_error or AgentEmptyOutputError("All retries exhausted without success")

    def call_codex(
        self,
        prompt: str,
        context: str = "",
        timeout: Optional[int] = None,
    ) -> str:
        """
        审查类阶段入口。默认与 call() 相同（方案一 claude --print）。
        仅当 ai.use_codex_cli=true 时才尝试 Codex CLI。
        """
        if not cfg_bool("ai.use_codex_cli", False):
            logger.debug("call_codex: use_codex_cli=false, using claude --print (subprocess_cli)")
            return self.call(prompt, context, timeout, headless=True)

        full_prompt = self._build_full_prompt(prompt, context)
        run_timeout = timeout if timeout is not None else self._codex_timeout
        start = time.time()
        model = self._claude_model or "default"

        # 1. 尝试配置的 CODEX_CMD（可选，非默认路径）
        if self._codex_cmd:
            out = self._try_codex_cmd(self._codex_cmd, full_prompt, run_timeout)
            if out:
                if is_enabled():
                    _try_log_llm(
                        model=model, prompt=full_prompt, output=out,
                        duration=time.time() - start,
                        metadata={"method": "codex-cmd"},
                    )
                return out
            logger.warning("Configured CODEX_CMD failed, falling back...")

        # 2. 尝试 PATH 中的 codex
        for cmd_tokens in (
            ["codex", "exec", "-a", "never", "--"],
            ["codex", "exec", "--"],
        ):
            out = self._try_codex_cmd(cmd_tokens, full_prompt, run_timeout)
            if out:
                if is_enabled():
                    _try_log_llm(
                        model=model, prompt=full_prompt, output=out,
                        duration=time.time() - start,
                        metadata={"method": "codex"},
                    )
                return out

        # 3. 回退 claude --print
        logger.info("Codex unavailable, falling back to claude --print")
        return self.call(prompt, context, timeout)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_full_prompt(self, prompt: str, context: str) -> str:
        if context:
            return f"[项目上下文]\n{context}\n\n[当前指令]\n{prompt}"
        return prompt

    def _invoke_claude_cli_subprocess(
        self,
        full_prompt: str,
        timeout: int,
        cwd: Optional[Path] = None,
        headless: bool = False,
        progress_workspace: Optional[Path] = None,
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> str:
        """subprocess 调用 `claude -p` + JSON 输出，返回 result 文本。"""
        raise_if_cancelled()
        cmd = self._build_claude_cmd(full_prompt, headless=headless)
        run_cwd = cwd if cwd else PROJECT_ROOT

        if progress_workspace is not None:
            return self._invoke_claude_cli_with_progress(
                cmd, timeout, run_cwd, progress_workspace,
                prompt_len=len(full_prompt),
                attempt=attempt, max_attempts=max_attempts,
            )

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, env=os.environ.copy(), cwd=str(run_cwd),
            )
        except subprocess.TimeoutExpired as exc:
            self._kill_stale_claude_cli()
            raise AgentTimeoutError(f"TIMEOUT after {timeout}s") from exc
        except FileNotFoundError as exc:
            raise AgentCliUnavailableError("claude command not found") from exc

        return self._finalize_claude_result(
            result.stdout or "", result.stderr or "", result.returncode,
            len(full_prompt), progress_workspace=None, elapsed_sec=0,
            attempt=attempt, max_attempts=max_attempts,
        )

    def _build_claude_cmd(self, full_prompt: str, *, headless: bool) -> list[str]:
        """构建 claude -p 命令（prompt 作为参数，不用 stdin）。"""
        out_fmt = cfg_str("ai.claude_output_format", "json").strip().lower()
        cmd: list[str] = ["claude", "-p", full_prompt, "--output-format", out_fmt or "json"]
        if self._claude_model:
            cmd += ["--model", self._claude_model]
        cmd.append("--no-session-persistence")

        use_headless = headless or cfg_bool("ai.claude_headless_print", True)
        if use_headless:
            cmd += [
                "--permission-mode",
                cfg_str("ai.claude_headless_permission_mode", "dontAsk"),
            ]
            tools = cfg_str("ai.claude_headless_tools", "Read,Glob,Grep").strip()
            if tools:
                cmd += ["--allowed-tools", tools]
            else:
                cmd += ["--tools", ""]
            cmd += ["--max-turns", str(cfg_int("ai.claude_headless_max_turns", 5))]
        else:
            cmd += [
                "--permission-mode",
                cfg_str("ai.claude_agent_permission_mode", "acceptEdits"),
            ]
            agent_tools = cfg_str("ai.claude_agent_tools", "Read,Edit,Write,Glob,Grep").strip()
            if agent_tools:
                cmd += ["--allowed-tools", agent_tools]
            disallowed = cfg_str("ai.claude_disallowed_tools", "Bash").strip()
            if disallowed:
                cmd += ["--disallowed-tools", disallowed]
            cmd += ["--max-turns", str(cfg_int("ai.claude_max_turns", 15))]
        return cmd

    def _invoke_claude_cli_with_progress(
        self, cmd: list[str], timeout: int, cwd: Path,
        workspace: Path, *, prompt_len: int, attempt: int, max_attempts: int,
    ) -> str:
        from utils.cli_progress import append_cli_log, init_cli_log, update_cli_progress

        init_cli_log(workspace, cmd)
        append_cli_log(workspace, "启动子进程 (claude -p json)…", stream="sys")
        deadline = time.time() + timeout
        stderr_done = threading.Event()

        def _drain_stderr(pipe) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    if line:
                        append_cli_log(workspace, line.rstrip(), stream="stderr")
            finally:
                stderr_done.set()

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=os.environ.copy(), cwd=str(cwd),
            )
        except FileNotFoundError as exc:
            raise AgentCliUnavailableError("claude command not found") from exc

        threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True).start()
        start = time.time()
        update_cli_progress(
            workspace, pid=proc.pid or 0, elapsed_sec=0, timeout_sec=timeout,
            attempt=attempt, max_attempts=max_attempts, running=True,
        )

        while True:
            raise_if_cancelled()
            elapsed = int(time.time() - start)
            if proc.poll() is not None:
                break
            if time.time() >= deadline:
                append_cli_log(workspace, f"超时 {timeout}s，终止进程", stream="sys")
                proc.kill()
                proc.wait(timeout=10)
                self._kill_stale_claude_cli()
                update_cli_progress(
                    workspace, pid=proc.pid or 0, elapsed_sec=elapsed, timeout_sec=timeout,
                    attempt=attempt, max_attempts=max_attempts, running=False, exit_code=-1,
                )
                raise AgentTimeoutError(f"TIMEOUT after {timeout}s")
            update_cli_progress(
                workspace, pid=proc.pid or 0, elapsed_sec=elapsed, timeout_sec=timeout,
                attempt=attempt, max_attempts=max_attempts, running=True,
            )
            time.sleep(2)

        stdout = (proc.stdout.read() if proc.stdout else "") or ""
        stderr_done.wait(timeout=5)
        return self._finalize_claude_result(
            stdout, "", proc.returncode if proc.returncode is not None else -1, prompt_len,
            progress_workspace=workspace, elapsed_sec=int(time.time() - start),
            attempt=attempt, max_attempts=max_attempts,
        )

    def _parse_claude_stdout(self, stdout: str, stderr: str, exit_code: int, prompt_len: int) -> str:
        raw = (stdout or "").strip()
        if not raw:
            self._classify_empty_output(stderr, exit_code, prompt_len)
            raise AgentEmptyOutputError("Empty stdout from claude CLI")

        fmt = cfg_str("ai.claude_output_format", "json").strip().lower()
        if fmt != "json":
            return raw

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("claude JSON parse failed, using raw stdout: %s", exc)
            return raw

        if data.get("is_error"):
            err = (
                data.get("api_error_status")
                or data.get("result")
                or f"cli error (exit={exit_code})"
            )
            self._classify_json_error(str(err), stderr)

        result = data.get("result", "")
        if isinstance(result, str) and result.strip():
            logger.info(
                "claude json: turns=%s cost_usd=%s duration_ms=%s result_len=%d",
                data.get("num_turns"),
                data.get("total_cost_usd"),
                data.get("duration_ms"),
                len(result),
            )
            return result
        self._classify_empty_output(stderr, exit_code, prompt_len)
        raise AgentEmptyOutputError("Empty result in claude JSON response")

    def _classify_json_error(self, err: str, stderr: str) -> None:
        combined = f"{err}\n{stderr}".lower()
        if "rate limit" in combined or "429" in combined:
            raise AgentRateLimitError(err[:300])
        if "context length" in combined or "maximum context" in combined:
            raise AgentContextLengthError(err[:300])
        raise AgentEmptyOutputError(err[:300])

    def _finalize_claude_result(
        self, stdout: str, stderr: str, exit_code: int, prompt_len: int, *,
        progress_workspace: Optional[Path], elapsed_sec: int,
        attempt: int = 1, max_attempts: int = 1,
    ) -> str:
        logger.info(
            "claude CLI: exit=%d raw_stdout=%d stderr=%d elapsed=%ds",
            exit_code, len(stdout or ""), len(stderr or ""), elapsed_sec,
        )
        if progress_workspace is not None:
            from utils.cli_progress import append_cli_log, update_cli_progress
            for line in (stderr or "").splitlines()[-20:]:
                append_cli_log(progress_workspace, line, stream="stderr")
            preview = (stdout or "")[:500]
            if preview:
                append_cli_log(progress_workspace, f"raw json {len(stdout)} chars", stream="sys")
            update_cli_progress(
                progress_workspace, pid=0, elapsed_sec=elapsed_sec, timeout_sec=0,
                attempt=attempt, max_attempts=max_attempts, running=False,
                stdout_len=len(stdout or ""), exit_code=exit_code,
            )
        if exit_code != 0 and not (stdout or "").strip():
            self._classify_empty_output(stderr, exit_code, prompt_len)
            raise AgentEmptyOutputError(
                f"Non-zero exit ({exit_code}): {(stderr or '')[:300]}"
            )
        return self._parse_claude_stdout(stdout, stderr, exit_code, prompt_len)

    # 兼容旧名称
    _try_call = _invoke_claude_cli_subprocess

    def _try_codex_cmd(self, cmd_tokens: list[str], full_prompt: str, timeout: int) -> Optional[str]:
        """可选：subprocess 调用 Codex CLI（与方案一并存，默认关闭）。"""
        try:
            if isinstance(cmd_tokens, str):
                parts = cmd_tokens.split()
            else:
                parts = list(cmd_tokens)

            result = subprocess.run(
                parts,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(PROJECT_ROOT),
                env=os.environ.copy(),
            )
            if result.returncode == 0 and (result.stdout or "").strip():
                logger.info("Codex CLI ok (%d chars)", len(result.stdout))
                return result.stdout
            logger.debug("Codex CLI failed: exit=%d, stderr=%s", result.returncode, result.stderr[:200])
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug("Codex CLI exception: %s", e)
        return None

    def _classify_empty_output(self, stderr: str, exit_code: int, prompt_len: int) -> None:
        """根据 stderr 内容对空输出进行分类并抛出对应异常。"""
        stderr_lower = stderr.lower()

        if "rate limit" in stderr_lower or "429" in stderr:
            raise AgentRateLimitError(f"Rate limited: {stderr[:300]}")

        if "context length" in stderr_lower or "maximum context" in stderr_lower:
            raise AgentContextLengthError(
                f"Context length exceeded (prompt={prompt_len} chars): {stderr[:300]}"
            )

        if exit_code != 0:
            raise AgentEmptyOutputError(f"Non-zero exit ({exit_code}): {stderr[:300]}")

    def _check_cli(self) -> bool:
        """检查 claude CLI 是否可用（缓存结果）。"""
        if self._version_checked:
            return self._version_ok
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                env=os.environ.copy(),
            )
            if result.returncode == 0:
                version_str = (result.stdout or result.stderr or "").strip()
                logger.info("Claude CLI version: %s", version_str)
                self._version_ok = True
            else:
                logger.error("Claude CLI --version failed")
                self._version_ok = False
        except FileNotFoundError:
            logger.error("Claude CLI not found in PATH")
            self._version_ok = False
        except Exception as e:
            logger.error("Claude CLI check error: %s", e)
            self._version_ok = False
        self._version_checked = True
        return self._version_ok

    def reset_version_check(self) -> None:
        """重置 CLI 检查缓存（主要用于测试）。"""
        self._version_checked = False
        self._version_ok = False

    @staticmethod
    def _kill_stale_claude_cli() -> None:
        """超时后清理残留 claude -p / --print 子进程（限当前用户 UID，避免误杀）。"""
        uid = str(os.getuid())
        for pattern in ("claude -p", "claude --print"):
            try:
                subprocess.run(
                    ["pkill", "-U", uid, "-f", pattern],
                    capture_output=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
