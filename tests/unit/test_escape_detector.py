"""
escape_detector.py 单元测试
覆盖：不可解检测、复杂度评估、L2 升级决策、核心文件检测
"""
import json
import sys
from pathlib import Path

import pytest

from utils.escape_detector import (
    detect_unsolvable,
    assess_complexity,
    should_escalate_to_l2,
    _extract_error_fingerprint,
    _extract_consensus_files,
)

class TestExtractErrorFingerprint:
    def test_json_errors(self):
        errors = [
            {"type": "unresolved_reference", "match": "e: Unresolved reference: Foo"},
            {"type": "null_safety", "match": "Only safe calls are allowed"},
        ]
        fp = _extract_error_fingerprint(json.dumps(errors))
        assert fp == ("null_safety", "unresolved_reference")

    def test_plain_text_fallback(self):
        fp = _extract_error_fingerprint("Some random error about NullPointerException")
        assert "NullPointerException" in fp or "Some" in fp

    def test_empty(self):
        fp = _extract_error_fingerprint("")
        assert fp == ("unknown",)

class TestDetectUnsolvable:
    def test_not_enough_history(self):
        ok, reason = detect_unsolvable([json.dumps([{"type": "a"}])])
        assert ok is False
        assert reason == ""

    def test_same_pattern_twice(self):
        err = json.dumps([{"type": "unresolved_reference"}])
        history = [err, err, err]
        ok, reason = detect_unsolvable(history)
        assert ok is True
        assert "unresolved_reference" in reason

    def test_different_patterns(self):
        history = [
            json.dumps([{"type": "a"}]),
            json.dumps([{"type": "b"}]),
            json.dumps([{"type": "c"}]),
        ]
        ok, reason = detect_unsolvable(history)
        assert ok is False

class TestAssessComplexity:
    def test_simple_l0(self):
        text = "| app/src/main/java/com/sport/Test.kt | 修改 | test |"
        result = assess_complexity(text)
        assert result["level"] == "L0"
        assert result["file_count"] == 1
        assert result["cross_module"] is False
        assert result["touches_core"] is False

    def test_cross_module_l2(self):
        text = (
            "| app/src/main/java/com/sport/A.kt | 修改 | a |\n"
            "| buildSrc/src/main/kotlin/Utils.kt | 修改 | b |\n"
            "| sport/src/main/java/com/sport/B.kt | 修改 | c |"
        )
        result = assess_complexity(text)
        assert result["level"] == "L2"
        assert result["cross_module"] is True
        assert len(result["reasons"]) > 0

    def test_core_file_l2(self):
        text = "| app/src/main/java/com/sport/KoinModule.kt | 修改 | di |"
        result = assess_complexity(text)
        assert result["level"] == "L2"
        assert result["touches_core"] is True

    def test_many_files_l2(self):
        lines = [f"| app/src/main/java/com/sport/File{i}.kt | 修改 | x |" for i in range(15)]
        result = assess_complexity("\n".join(lines))
        assert result["level"] == "L2"
        assert result["file_count"] == 15

class TestShouldEscalateToL2:
    def test_already_l2_noop(self, tmp_path: Path):
        assert should_escalate_to_l2("L2", tmp_path / "nonexistent") == (False, [])

    def test_no_consensus_file(self, tmp_path: Path):
        assert should_escalate_to_l2("L1", tmp_path / "nonexistent") == (False, [])

    def test_escalate_on_complexity(self, tmp_path: Path):
        consensus = tmp_path / "consensus.md"
        consensus.write_text(
            "| app/src/main/java/com/sport/KoinModule.kt | 修改 | di |\n",
            encoding="utf-8",
        )
        should, reasons = should_escalate_to_l2("L1", consensus)
        assert should is True
        assert len(reasons) > 0

    def test_no_escalate_simple(self, tmp_path: Path):
        consensus = tmp_path / "consensus.md"
        consensus.write_text(
            "| app/src/main/java/com/sport/Test.kt | 修改 | test |\n",
            encoding="utf-8",
        )
        should, reasons = should_escalate_to_l2("L1", consensus)
        assert should is False
        assert reasons == []

class TestExtractConsensusFiles:
    def test_extracts_paths(self):
        text = (
            "| 文件 | 操作 | 说明 |\n"
            "| app/src/main/java/A.kt | 修改 | a |\n"
            "| buildSrc/B.kt | 新增 | b |\n"
            "| not_a_path | 修改 | c |\n"
        )
        files = _extract_consensus_files(text)
        assert "app/src/main/java/A.kt" in files
        assert "buildSrc/B.kt" in files
        assert "not_a_path" not in files