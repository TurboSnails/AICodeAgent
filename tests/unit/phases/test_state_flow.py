"""
Review 阶段状态流转测试
覆盖：CodexReview -> RED_TEAM_REVIEW、RedTeam -> REQUIREMENT_REVIEW、Requirement -> GIT_COMMITTING
以及各阶段失败时进入 CORRECTING
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.exceptions import AgentRecoverableError
from engine.state_machine import State, Task
from phases.codex_review import CodexReviewHandler
from phases.red_team_review import RedTeamReviewHandler
from phases.requirement_review import RequirementReviewHandler


class TestCodexReviewFlow:
    def test_pass_goes_to_architect_review(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nPASS"
        handler = CodexReviewHandler(ai_client=ai)
        task = Task(
            task_id="c1", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.codex_review.save_task"):
            with patch("phases.codex_review.cfg_bool", return_value=True):
                result = handler.handle(task, tmp_path)
        assert result.next_state == State.ARCHITECT_REVIEW
        assert "codex_report" in result.artifacts

    def test_pass_with_architect_disabled_skips_to_red_team(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nPASS"
        handler = CodexReviewHandler(ai_client=ai)
        task = Task(
            task_id="c1b", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.codex_review.save_task"):
            with patch("phases.codex_review.cfg_bool", return_value=False):
                result = handler.handle(task, tmp_path)
        assert result.next_state == State.RED_TEAM_REVIEW
        assert "codex_report" in result.artifacts

    def test_fail_goes_to_correcting(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nFAIL\nLogic issues\n- null pointer"
        handler = CodexReviewHandler(ai_client=ai)
        task = Task(
            task_id="c2", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.codex_review.save_task"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING
        assert "fix_prompt" in result.artifacts

    def test_missing_ai_raises_recoverable(self, tmp_path: Path):
        handler = CodexReviewHandler(ai_client=None)
        task = Task(
            task_id="c3", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="",
        )
        with pytest.raises(AgentRecoverableError, match="missing ai_client"):
            handler.handle(task, tmp_path)


class TestRedTeamReviewFlow:
    def test_pass_goes_to_requirement_review(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nPASS"
        handler = RedTeamReviewHandler(ai_client=ai)
        task = Task(
            task_id="r1", raw_requirement="add login", level="L2",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.red_team_review.save_task"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.REQUIREMENT_REVIEW

    def test_disabled_skips_to_requirement_review(self, tmp_path: Path):
        ai = MagicMock()
        handler = RedTeamReviewHandler(ai_client=ai)
        task = Task(
            task_id="r2", raw_requirement="add login", level="L2",
            site_hint="", source="test", chat_id="",
        )
        with patch("phases.red_team_review.cfg_bool", return_value=False):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.REQUIREMENT_REVIEW
        ai.call_codex.assert_not_called()

    def test_level_not_allowed_skips(self, tmp_path: Path):
        ai = MagicMock()
        handler = RedTeamReviewHandler(ai_client=ai)
        task = Task(
            task_id="r3", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="",
        )
        with patch("phases.red_team_review.cfg_str", return_value="L2"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.REQUIREMENT_REVIEW
        ai.call_codex.assert_not_called()


class TestRequirementReviewFlow:
    def test_pass_goes_to_git_committing(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nPASS"
        handler = RequirementReviewHandler(ai_client=ai)
        task = Task(
            task_id="a1", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.requirement_review.save_task"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.GIT_COMMITTING

    def test_fail_goes_to_correcting(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nFAIL\nMissing feature"
        handler = RequirementReviewHandler(ai_client=ai)
        task = Task(
            task_id="a2", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.requirement_review.save_task"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING
        assert "fix_prompt" in result.artifacts

    def test_uses_phase_counters_for_round(self, tmp_path: Path):
        ai = MagicMock()
        ai.call_codex.return_value = "## Verdict\nFAIL\nMissing"
        handler = RequirementReviewHandler(ai_client=ai)
        task = Task(
            task_id="a3", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="",
            phase_counters={"acceptance": 1},
        )
        with patch("phases.requirement_review.save_task") as mock_save:
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING
        # 确认 phase_counters 被持久化
        saved_task = mock_save.call_args[0][0]
        assert saved_task.phase_counters["acceptance"] == 2


class TestSelfReviewFallback:
    """Self Review confidence 回退兜底测试"""

    def test_high_confidence_no_critical_treats_as_pass(self, tmp_path: Path):
        """Verdict 缺失但 confidence 足够且无严重问题 → 视为 PASS"""
        ai = MagicMock()
        ai.call.return_value = "## Confidence Score\n8\nSome minor style issues"
        handler = __import__("phases.self_review", fromlist=["SelfReviewHandler"]).SelfReviewHandler(ai_client=ai)
        task = Task(
            task_id="s1", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.self_review.list_task_relevant_changed_files", return_value=["a.kt"]):
            with patch("phases.self_review.workspace_context", return_value=""):
                with patch("phases.self_review.cfg_bool", return_value=True):
                    with patch("phases.self_review.cfg_int", return_value=7):
                        result = handler.handle(task, tmp_path)
        assert result.next_state == State.CODEX_REVIEW

    def test_high_confidence_with_critical_still_fails(self, tmp_path: Path):
        """Verdict 缺失但报告中有 MUST → 仍应 FAIL"""
        ai = MagicMock()
        ai.call.return_value = "## Confidence Score\n8\n- MUST fix null pointer"
        handler = __import__("phases.self_review", fromlist=["SelfReviewHandler"]).SelfReviewHandler(ai_client=ai)
        task = Task(
            task_id="s2", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.self_review.list_task_relevant_changed_files", return_value=["a.kt"]):
            with patch("phases.self_review.workspace_context", return_value=""):
                with patch("phases.self_review.cfg_bool", return_value=True):
                    with patch("phases.self_review.cfg_int", return_value=7):
                        with patch("phases.self_review.save_task"):
                            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING

    def test_low_confidence_always_fails(self, tmp_path: Path):
        """Verdict 缺失且 confidence 低于阈值 → FAIL"""
        ai = MagicMock()
        ai.call.return_value = "## Confidence Score\n5\nSome issues"
        handler = __import__("phases.self_review", fromlist=["SelfReviewHandler"]).SelfReviewHandler(ai_client=ai)
        task = Task(
            task_id="s3", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.self_review.list_task_relevant_changed_files", return_value=["a.kt"]):
            with patch("phases.self_review.workspace_context", return_value=""):
                with patch("phases.self_review.cfg_bool", return_value=True):
                    with patch("phases.self_review.cfg_int", return_value=7):
                        with patch("phases.self_review.save_task"):
                            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING


class TestArchitectMustFallback:
    """Architect Review MUST 级别问题兜底强制 FAIL 测试"""

    def test_must_items_force_fail_despite_pass_verdict(self, tmp_path: Path):
        """Verdict 为 PASS 但报告中有 MUST → 强制 FAIL"""
        ai = MagicMock()
        ai.call.return_value = "## Verdict\nPASS\n\n## Architecture Issues\n- MUST: God class detected"
        handler = __import__("phases.architect_review", fromlist=["ArchitectReviewHandler"]).ArchitectReviewHandler(ai_client=ai)
        task = Task(
            task_id="ar1", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.architect_review.list_changed_files", return_value=["a.kt"]):
            with patch("phases.architect_review.read_changed_sources", return_value="code"):
                with patch("phases.architect_review.workspace_context", return_value=""):
                    with patch("phases.architect_review.cfg_bool", return_value=True):
                        with patch("phases.architect_review.cfg_str", return_value="L0"):
                            with patch("phases.architect_review.cfg_int", return_value=1):
                                with patch("phases.architect_review.save_task"):
                                    result = handler.handle(task, tmp_path)
        assert result.next_state == State.CORRECTING

    def test_pass_verdict_no_must_succeeds(self, tmp_path: Path):
        """Verdict 为 PASS 且无 MUST → 正常通过"""
        ai = MagicMock()
        ai.call.return_value = "## Verdict\nPASS\n\n## Architecture Issues\n- 无"
        handler = __import__("phases.architect_review", fromlist=["ArchitectReviewHandler"]).ArchitectReviewHandler(ai_client=ai)
        task = Task(
            task_id="ar2", raw_requirement="add login", level="L0",
            site_hint="", source="test", chat_id="", base_branch="main",
        )
        with patch("phases.architect_review.list_changed_files", return_value=["a.kt"]):
            with patch("phases.architect_review.read_changed_sources", return_value="code"):
                with patch("phases.architect_review.workspace_context", return_value=""):
                    with patch("phases.architect_review.cfg_bool", return_value=True):
                        with patch("phases.architect_review.cfg_str", return_value="L0"):
                            with patch("phases.architect_review.cfg_int", return_value=1):
                                result = handler.handle(task, tmp_path)
        assert result.next_state == State.RED_TEAM_REVIEW
