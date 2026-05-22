"""
BuildService 单元测试
覆盖：构建超时、错误解析、clean 调用
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.exceptions import BuildFailureError
from services.build_service import BuildService

class TestBuildTimeout:
    """构建超时处理"""

    def test_timeout_returns_error_code(self, tmp_path: Path):
        import subprocess
        service = BuildService(project_root=tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["./gradlew", "app:assembleDebug"], timeout=900
            )
            code, out, err = service._run_gradle(["app:assembleDebug"])
            assert code == -1
            assert "timeout" in err.lower()

class TestErrorParsing:
    """错误日志解析"""

    def test_parse_returns_last_lines_when_script_missing(self, tmp_path: Path):
        service = BuildService(project_root=tmp_path)
        log = "line1\nline2\nline3\n...\nERROR: compilation failed"
        result = service.parse_errors(log)
        assert "ERROR: compilation failed" in result

    def test_parse_empty_log(self, tmp_path: Path):
        service = BuildService(project_root=tmp_path)
        result = service.parse_errors("")
        assert result == ""

class TestBuildFlow:
    """构建流程"""

    def test_build_stores_log(self, tmp_path: Path):
        service = BuildService(project_root=tmp_path)

        with patch.object(service, "_run_gradle") as mock_gradle:
            mock_gradle.return_value = (0, "BUILD SUCCESSFUL", "")
            success, log = service.build("task1", tmp_path)
            assert success is True
            assert "BUILD SUCCESSFUL" in log
            assert (tmp_path / "build.log").exists()

    def test_build_failure_raises(self, tmp_path: Path):
        service = BuildService(project_root=tmp_path)

        with patch.object(service, "_run_gradle") as mock_gradle:
            mock_gradle.return_value = (1, "", "Compilation error")
            with pytest.raises(BuildFailureError):
                service.build("task1", tmp_path)

    def test_assemble_only_skips_tests(self, tmp_path: Path):
        import json
        from utils.project_guides import BuildPolicy, write_build_policy_files

        service = BuildService(project_root=tmp_path)
        policy = BuildPolicy(
            source="CLAUDE.md",
            assemble_only=True,
            gradle_tasks=["app:assembleDebug"],
        )
        write_build_policy_files(tmp_path, policy)

        with patch.object(service, "_run_gradle") as mock_gradle:
            mock_gradle.return_value = (0, "BUILD SUCCESSFUL", "")
            success, log = service.build("task1", tmp_path, level="L0", requirement="编译正常")
            assert success is True
            assert mock_gradle.call_count == 1
            assert mock_gradle.call_args[0][0] == ["app:assembleDebug", "--console=plain"]
            log_text = (tmp_path / "build.log").read_text()
            assert "build policy" in log_text
            assert json.loads((tmp_path / "build_policy.json").read_text())["assemble_only"] is True