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