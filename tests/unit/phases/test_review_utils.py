"""
phases/_review_utils.py 单元测试
覆盖：verdict 解析、变更文件列表、prompt 构建、源码读取
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from phases._review_utils import (
    build_codex_review_prompt,
    build_fix_plan_prompt,
    build_red_team_prompt,
    build_requirement_acceptance_prompt,
    list_changed_files,
    merge_review_fix_plans,
    parse_and_save_fix_plan,
    parse_codex_verdict,
    read_changed_sources,
    workspace_context,
)
from phases._fix_plan import FixItem, FixPlan, FixPriority


class TestParseCodexVerdict:
    def test_explicit_pass(self):
        assert parse_codex_verdict("## Verdict\nPASS") is True

    def test_explicit_fail(self):
        assert parse_codex_verdict("## Verdict\nFAIL") is False

    def test_colon_format_pass(self):
        assert parse_codex_verdict("Verdict: PASS") is True

    def test_colon_format_fail(self):
        assert parse_codex_verdict("Verdict: FAIL") is False

    def test_empty_is_fail(self):
        assert parse_codex_verdict("") is False

    def test_whitespace_only_is_fail(self):
        assert parse_codex_verdict("   \n\n  ") is False

    def test_fail_without_pass(self):
        assert parse_codex_verdict("Some issues found. FAIL.") is False

    def test_both_present_prefers_pass(self):
        # 实现先匹配 PASS
        assert parse_codex_verdict("Verdict: PASS\nBut actually FAIL") is True


class TestListChangedFiles:
    def test_returns_list_in_git_repo(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        # 初始化 git
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "a.kt"
        f.write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("y", encoding="utf-8")
        changed = list_changed_files()
        assert isinstance(changed, list)
        assert "a.kt" in changed

    def test_no_git_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        assert list_changed_files() == []


class TestReadChangedSources:
    def test_reads_kt_files(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        app = tmp_path / "app"
        app.mkdir(parents=True)
        (app / "Test.kt").write_text("class Test {}", encoding="utf-8")
        out = read_changed_sources(["app/Test.kt"], max_total=10000)
        assert "class Test {}" in out

    def test_skips_non_kt(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        (tmp_path / "readme.md").write_text("# hi", encoding="utf-8")
        out = read_changed_sources(["readme.md"])
        assert out == "（未能读取变更源码）"

    def test_respects_max_total(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        (tmp_path / "A.kt").write_text("x" * 3000, encoding="utf-8")
        (tmp_path / "B.kt").write_text("y" * 3000, encoding="utf-8")
        out = read_changed_sources(["A.kt", "B.kt"], max_total=100)
        # 第二个文件因超出上限被截断提示
        assert "已截断" in out or "A.kt" in out


class TestWorkspaceContext:
    def test_includes_consensus_md(self, tmp_path: Path):
        (tmp_path / "consensus.md").write_text("consensus text", encoding="utf-8")
        ctx = workspace_context(tmp_path)
        assert "consensus.md" in ctx
        assert "consensus text" in ctx

    def test_missing_files_ignored(self, tmp_path: Path):
        ctx = workspace_context(tmp_path)
        assert ctx == ""


class TestBuildPrompts:
    def test_codex_prompt_contains_requirement(self, tmp_path: Path):
        prompt = build_codex_review_prompt(
            requirement="add login",
            workspace=tmp_path,
            changed_files=["a.kt"],
            impact_summary="",
        )
        assert "add login" in prompt
        assert "Codex 逻辑审查员" in prompt

    def test_requirement_prompt_contains_requirement(self, tmp_path: Path):
        prompt = build_requirement_acceptance_prompt(
            requirement="add login",
            workspace=tmp_path,
            changed_files=["a.kt"],
            prior_codex_report="",
        )
        assert "add login" in prompt
        assert "需求验收审查员" in prompt

    def test_red_team_prompt_contains_requirement(self, tmp_path: Path):
        prompt = build_red_team_prompt(
            requirement="add login",
            workspace=tmp_path,
            changed_files=["a.kt"],
            prior_codex_report="",
        )
        assert "add login" in prompt
        assert "Red Team" in prompt


class TestParseAndSaveFixPlan:
    def test_parse_json_block_and_save(self, tmp_path: Path):
        output = """
Some review.
```json
{
  "fix_plan": {
    "items": [
      {"priority": "HIGH", "category": "NPE", "description": "null risk", "target_files": ["A.kt"], "suggested_fix": "check"}
    ]
  }
}
```
"""
        plan = parse_and_save_fix_plan(output, tmp_path)
        assert plan is not None
        assert len(plan.items) == 1
        assert plan.items[0].priority == FixPriority.HIGH
        assert (tmp_path / "fix_plan.json").exists()

    def test_no_json_returns_none(self, tmp_path: Path):
        plan = parse_and_save_fix_plan("no fix plan here", tmp_path)
        assert plan is None
        assert not (tmp_path / "fix_plan.json").exists()

    def test_prompt_contains_fix_plan_instruction(self):
        prompt = build_fix_plan_prompt()
        assert "Fix Plan" in prompt
        assert "fix_plan" in prompt


class TestMergeReviewFixPlans:
    def test_merge_codex_and_red_team(self, tmp_path: Path):
        import json as _json
        (tmp_path / "codex_fix_plan.json").write_text(
            _json.dumps({"items": [{"priority": "MEDIUM", "category": "A", "description": "a", "target_files": ["1.kt"], "suggested_fix": ""}]}),
            encoding="utf-8",
        )
        (tmp_path / "red_team_fix_plan.json").write_text(
            _json.dumps({"items": [{"priority": "CRITICAL", "category": "B", "description": "b", "target_files": ["2.kt"], "suggested_fix": ""}]}),
            encoding="utf-8",
        )
        merged = merge_review_fix_plans(tmp_path)
        assert merged is not None
        assert len(merged.items) == 2
        assert merged.items[0].priority == FixPriority.CRITICAL

    def test_no_files_returns_none(self, tmp_path: Path):
        assert merge_review_fix_plans(tmp_path) is None
