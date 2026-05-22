"""
CodingHandler 单元测试
覆盖：代码应用、黑名单拦截、空输出处理
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.exceptions import AgentRecoverableError
from engine.state_machine import State, Task
from phases.coding import CodingHandler

class TestCodeApplication:
    """代码变更应用"""

    def test_apply_valid_code(self, tmp_path: Path):
        git = MagicMock()
        git.create_agent_branch.return_value = "feature/agent-test123"
        git.apply_code_changes.return_value = (
            ["app/src/main/java/Test.kt"],
            [],
        )

        ai = MagicMock()
        ai.call.return_value = """
=== FILE: app/src/main/java/Test.kt ===
class Test {}
=== END FILE ===
"""

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="test123", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
            base_branch="main",
        )

        result = handler.handle(task, tmp_path)

        assert result.next_state == State.BUILDING
        assert "app/src/main/java/Test.kt" in result.artifacts["applied_files"]
        ai.call.assert_called_once()
        git.apply_code_changes.assert_called_once()

    def test_empty_output_raises_recoverable(self, tmp_path: Path):
        git = MagicMock()
        ai = MagicMock()
        ai.call.return_value = "   "

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="empty1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )

        with patch("phases.coding.save_task"):
            with pytest.raises(AgentRecoverableError, match="empty output"):
                handler.handle(task, tmp_path)

    def test_no_files_applied_raises_recoverable(self, tmp_path: Path):
        git = MagicMock()
        git.apply_code_changes.return_value = ([], [])
        git.list_worktree_changed_paths.return_value = []
        git.partition_changed_paths.return_value = ([], [])

        ai = MagicMock()
        ai.call.return_value = "some output without file markers"

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="nofile1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )

        with patch("phases.coding.save_task"):
            with pytest.raises(AgentRecoverableError, match="FILE"):
                handler.handle(task, tmp_path)

    def test_blocked_files_recorded_but_not_failing(self, tmp_path: Path):
        git = MagicMock()
        git.create_agent_branch.return_value = "feature/agent-test456"
        git.apply_code_changes.return_value = (
            ["app/src/main/java/Test.kt"],
            [(".github/workflows/ci.yml", "命中安全黑名单")],
        )

        ai = MagicMock()
        ai.call.return_value = """
=== FILE: app/src/main/java/Test.kt ===
class Test {}
=== END FILE ===
=== FILE: .github/workflows/ci.yml ===
name: CI
=== END FILE ===
"""

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="test456", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
            base_branch="main",
        )

        result = handler.handle(task, tmp_path)

        assert result.next_state == State.BUILDING
        assert len(result.artifacts["blocked_files"]) == 1
        # 被拦截文件不应导致失败

class TestBranchCreation:
    """分支创建"""

    def test_creates_branch_when_missing(self, tmp_path: Path):
        git = MagicMock()
        git.get_current_branch.return_value = "main"
        git.create_agent_branch.return_value = "feature/agent-branch1"
        git.apply_code_changes.return_value = (["app/Test.kt"], [])

        ai = MagicMock()
        ai.call.return_value = """
=== FILE: app/Test.kt ===
class Test {}
=== END FILE ===
"""

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="branch1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )

        handler.handle(task, tmp_path)

        git.create_agent_branch.assert_called_once_with("branch1", "main")

    def test_reuses_existing_branch(self, tmp_path: Path):
        git = MagicMock()
        git.apply_code_changes.return_value = (["app/Test.kt"], [])

        ai = MagicMock()
        ai.call.return_value = """
=== FILE: app/Test.kt ===
class Test {}
=== END FILE ===
"""

        handler = CodingHandler(ai_client=ai, git_service=git)
        task = Task(
            task_id="branch2", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
            branch="feature/agent-branch2",
            base_branch="develop",
        )

        handler.handle(task, tmp_path)

        git.create_agent_branch.assert_not_called()