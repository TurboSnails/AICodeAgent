#!/usr/bin/env python3
"""
构建服务 — V4 重构
封装 Gradle 构建与测试调用，错误解析。
- assembleDebug + testDebugUnitTest + lintDebug
- 错误日志持久化
- 调用 course_correct.py 解析错误
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

from engine.exceptions import BuildFailureError
from utils.config_loader import cfg_int
from utils.logging_config import get_logger

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT


class BuildService:
    """
    Gradle 构建封装。

    职责：
    1. 执行 gradle assembleDebug / testDebugUnitTest / lintDebug
    2. 收集并保存构建日志
    3. 解析错误摘要（调用 course_correct.py）
    """

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or PROJECT_ROOT

    @property
    def _build_timeout(self) -> int:
        return cfg_int("timeouts.build", 900)

    def build(self, task_id: str, workspace: Path) -> Tuple[bool, str]:
        """
        执行完整构建流程。

        :return: (是否成功, 完整日志文本)
        :raises BuildFailureError: 构建失败时
        """
        log_path = workspace / "build.log"
        log_parts = []

        # 1. assembleDebug
        code, stdout, stderr = self._run_gradle(["app:assembleDebug", "--console=plain"])
        log_parts.append(f"=== assembleDebug ===\n{stdout}\n{stderr}\n")
        if code != 0:
            log_path.write_text("".join(log_parts), encoding="utf-8")
            raise BuildFailureError(f"assembleDebug failed (exit={code})")

        # 2. testDebugUnitTest
        code, stdout, stderr = self._run_gradle(["testDebugUnitTest", "--console=plain"])
        log_parts.append(f"\n=== testDebugUnitTest ===\n{stdout}\n{stderr}\n")
        if code != 0:
            log_path.write_text("".join(log_parts), encoding="utf-8")
            raise BuildFailureError(f"testDebugUnitTest failed (exit={code})")

        # 3. lintDebug
        code, stdout, stderr = self._run_gradle(["lintDebug", "--console=plain"])
        log_parts.append(f"\n=== lintDebug ===\n{stdout}\n{stderr}\n")
        log_path.write_text("".join(log_parts), encoding="utf-8")
        if code != 0:
            raise BuildFailureError(f"lintDebug failed (exit={code})")

        return True, "".join(log_parts)

    def clean(self) -> None:
        """执行 gradle clean。"""
        code, out, err = self._run_gradle(["clean", "--console=plain"])
        if code != 0:
            logger.warning("gradle clean failed: %s", err[:500])
        else:
            logger.info("gradle clean done")

    def parse_errors(self, log: str) -> str:
        """调用 course_correct.py 解析 Gradle 错误。"""
        script = self.project_root / "AICodeAgent" / "scripts" / "course_correct.py"
        if not script.exists():
            return "\n".join(log.splitlines()[-30:])

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write(log)
            log_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(script), "--log", log_file, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self.project_root),
                env=os.environ.copy(),
            )
            if result.returncode == 0:
                return result.stdout
        except Exception as e:
            logger.warning("course_correct.py failed: %s", e)
        finally:
            os.unlink(log_file)

        return "\n".join(log.splitlines()[-30:])

    def _run_gradle(self, args: list[str]) -> Tuple[int, str, str]:
        cmd = ["./gradlew"] + args
        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=self._build_timeout,
                env=os.environ.copy(),
            )
            if result.stdout:
                logger.info(result.stdout[-3000:])
            if result.returncode != 0 and result.stderr:
                logger.warning(result.stderr[-2000:])
            return result.returncode, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            logger.error("Gradle TIMEOUT after %ds", self._build_timeout)
            return -1, "", f"timeout after {self._build_timeout}s"
