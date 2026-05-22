#!/usr/bin/env python3
"""
统一 AI 客户端 — V4 重构
封装 claude --print / Codex CLI / 回退逻辑
- 指数退避重试
- 错误分类（rate limit / context length / timeout）
- 惰性加载配置（模块顶层不触发 IO）
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from engine.exceptions import (
    AgentCliUnavailableError,
    AgentContextLengthError,
    AgentEmptyOutputError,
    AgentRateLimitError,
    AgentTimeoutError,
)
from utils.config_loader import cfg_float, cfg_int, cfg_str
from utils.logging_config import get_logger

logger = get_logger(__name__)

from utils.paths import PROJECT_ROOT


class AIClient:
    """
    统一 LLM 调用客户端。

    职责：
    1. 管理 claude CLI / Codex CLI 的调用与回退
    2. 实现指数退避重试
    3. 分类错误并抛出结构化异常
    4. 惰性读取配置（每次调用时读取，支持热更新）
    """

    def __init__(self):
        self._version_checked = False
        self._version_ok = False

    # ------------------------------------------------------------------
    # 配置属性（惰性加载）
    # ------------------------------------------------------------------

    @property
    def _claude_model(self) -> str:
        return cfg_str("ai.claude_model", "")

    @property
    def _claude_timeout(self) -> int:
        return cfg_int("timeouts.claude_code", 1800)

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
    ) -> str:
        """
        统一调用入口。

        :param prompt: 当前指令
        :param context: 项目上下文（会被拼接到 prompt 前面）
        :param timeout: 覆盖默认超时
        :return: 模型输出的文本
        :raises: AgentCliUnavailableError, AgentContextLengthError,
                 AgentRateLimitError, AgentTimeoutError, AgentEmptyOutputError
        """
        if not self._check_cli():
            raise AgentCliUnavailableError("Claude CLI not found in PATH")

        full_prompt = self._build_full_prompt(prompt, context)
        run_timeout = timeout if timeout is not None else self._claude_timeout

        logger.info(
            "AIClient.call start: model=%s, prompt_len=%d, timeout=%ds",
            self._claude_model or "(default)",
            len(full_prompt),
            run_timeout,
        )

        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                return self._try_call(full_prompt, run_timeout)
            except (AgentRateLimitError, AgentTimeoutError, AgentEmptyOutputError) as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = (2 ** attempt) * self._base_delay
                    logger.info("Retrying in %.1fs... (%s)", delay, e)
                    time.sleep(delay)
                else:
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
        优先使用 Codex CLI（若配置），失败回退到 claude --print。
        供 review 类阶段使用。
        """
        full_prompt = self._build_full_prompt(prompt, context)
        run_timeout = timeout if timeout is not None else self._codex_timeout

        # 1. 尝试配置的 CODEX_CMD
        if self._codex_cmd:
            out = self._try_codex_cmd(self._codex_cmd, full_prompt, run_timeout)
            if out:
                return out
            logger.warning("Configured CODEX_CMD failed, falling back...")

        # 2. 尝试 PATH 中的 codex
        for cmd_tokens in (
            ["codex", "exec", "-a", "never", "--"],
            ["codex", "exec", "--"],
        ):
            out = self._try_codex_cmd(cmd_tokens, full_prompt, run_timeout)
            if out:
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

    def _try_call(self, full_prompt: str, timeout: int) -> str:
        """单次调用 claude --print，返回输出或抛出结构化异常。"""
        cmd = ["claude", "--print"]
        if self._claude_model:
            cmd += ["--model", self._claude_model]

        env = os.environ.copy()
        try:
            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(PROJECT_ROOT),
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeoutError(f"TIMEOUT after {timeout}s") from exc
        except FileNotFoundError as exc:
            raise AgentCliUnavailableError("claude command not found") from exc

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        logger.info(
            "claude --print: exit_code=%d, stdout_len=%d, stderr_len=%d",
            result.returncode,
            len(stdout),
            len(stderr),
        )

        # 空输出处理 — 分类错误原因
        if not stdout.strip():
            self._classify_empty_output(stderr, result.returncode, len(full_prompt))
            # 如果上面没有 raise，按可恢复错误处理
            raise AgentEmptyOutputError("Empty stdout from claude --print")

        return stdout

    def _try_codex_cmd(self, cmd_tokens: list[str], full_prompt: str, timeout: int) -> Optional[str]:
        """尝试 Codex CLI 调用，返回输出或 None（不抛异常）。"""
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
