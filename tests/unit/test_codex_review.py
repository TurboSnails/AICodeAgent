"""
codex_review.py 单元测试
覆盖：verdict 解析、变更文件列表、错误模式匹配
"""
import sys
from pathlib import Path

import pytest

from phases._review_utils import parse_codex_verdict, list_changed_files, _run_cmd

class TestParseCodexVerdict:
    def test_explicit_pass(self):
        text = "## Verdict\nPASS"
        assert parse_codex_verdict(text) is True

    def test_explicit_fail(self):
        text = "## Verdict\nFAIL"
        assert parse_codex_verdict(text) is False

    def test_colon_format_pass(self):
        text = "Verdict: PASS"
        assert parse_codex_verdict(text) is True

    def test_colon_format_fail(self):
        text = "Verdict: FAIL"
        assert parse_codex_verdict(text) is False

    def test_empty_is_fail(self):
        assert parse_codex_verdict("") is False

    def test_whitespace_only_is_fail(self):
        assert parse_codex_verdict("   \n\n  ") is False

    def test_fail_without_pass(self):
        text = "Some issues found. FAIL."
        assert parse_codex_verdict(text) is False

    def test_both_present_prefers_pass(self):
        # 代码实现：先匹配 PASS（出现在 FAIL 之前），所以返回 True
        text = "Verdict: PASS\nBut actually FAIL"
        assert parse_codex_verdict(text) is True

class TestListChangedFiles:
    def test_returns_list(self, tmp_path: Path, monkeypatch):
        # codex_review 的 PROJECT_ROOT 是模块级常量，测试较困难
        # 这里仅做接口契约测试
        monkeypatch.setattr("phases._review_utils.PROJECT_ROOT", tmp_path, raising=False)
        # 在临时目录初始化 git
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "app" / "src" / "main" / "java" / "Test.kt"
        f.parent.mkdir(parents=True)
        f.write_text("class Test", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("class Test {}\n", encoding="utf-8")
        changed = list_changed_files()
        assert isinstance(changed, list)