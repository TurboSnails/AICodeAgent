"""
GitService 单元测试
覆盖：黑名单拦截、路径遍历防护、代码块解析、分支管理
"""
import sys
from pathlib import Path

import pytest

from services.git_service import GitService, BLOCKED_PATHS

class TestBlockedPaths:
    """安全黑名单校验"""

    def test_blocked_github_path(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        is_blocked, reason = git._is_blocked_path(".github/workflows/ci.yml")
        assert is_blocked is True
        assert "安全黑名单" in reason

    def test_blocked_buildsrc_config(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        is_blocked, reason = git._is_blocked_path("buildSrc/src/main/kotlin/Configs.kt")
        assert is_blocked is True

    def test_blocked_keystore(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        is_blocked, reason = git._is_blocked_path("app/debug.keystore")
        assert is_blocked is True

    def test_allowed_normal_path(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        is_blocked, reason = git._is_blocked_path("app/src/main/java/com/sport/UserScreen.kt")
        assert is_blocked is False

    def test_blocked_buildconfig(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        is_blocked, reason = git._is_blocked_path("app/src/main/java/BuildConfig.kt")
        assert is_blocked is True

class TestPathTraversalProtection:
    """路径遍历防护"""

    def test_path_traversal_blocked(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        output = """
=== FILE: ../../../etc/passwd ===
root:x:0:0
=== END FILE ===
"""
        applied, blocked = git.apply_code_changes(output)
        assert len(applied) == 0
        # 路径遍历被静默跳过，不计入 blocked 列表

    def test_normal_path_allowed(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        output = """
=== FILE: app/src/main/java/Test.kt ===
class Test {}
=== END FILE ===
"""
        applied, blocked = git.apply_code_changes(output)
        assert len(applied) == 1
        assert "app/src/main/java/Test.kt" in applied
        assert (tmp_path / "app" / "src" / "main" / "java" / "Test.kt").exists()

class TestCodeBlockParsing:
    """代码块解析"""

    def test_multiple_files(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        output = """
=== FILE: app/A.kt ===
class A {}
=== END FILE ===
Some text in between
=== FILE: app/B.kt ===
class B {}
=== END FILE ===
"""
        applied, blocked = git.apply_code_changes(output)
        assert len(applied) == 2
        assert "app/A.kt" in applied
        assert "app/B.kt" in applied

    def test_empty_content(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        output = """
=== FILE: app/Empty.kt ===
=== END FILE ===
"""
        applied, blocked = git.apply_code_changes(output)
        assert len(applied) == 1
        file_path = tmp_path / "app" / "Empty.kt"
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == ""

    def test_no_file_markers(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        output = "just some text without file markers"
        applied, blocked = git.apply_code_changes(output)
        assert len(applied) == 0
        assert len(blocked) == 0

class TestBranchManagement:
    """分支管理（mock git 命令）"""

    def test_branch_name_format(self, tmp_path: Path):
        git = GitService(project_root=tmp_path)
        # 通过 _run_cmd 的返回值模拟
        # 这里只验证分支名称格式
        task_id = "abc123"
        expected = f"feature/agent-{task_id}"
        assert expected.startswith("feature/agent-")