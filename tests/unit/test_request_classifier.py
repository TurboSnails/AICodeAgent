#!/usr/bin/env python3
"""
RequestClassifier 单元测试
"""

from unittest.mock import MagicMock, patch

import pytest

from services.request_classifier import RequestClassifier, ClassificationResult


@pytest.fixture
def classifier():
    return RequestClassifier()


class TestExplainClassification:
    def test_explain_how_does(self, classifier):
        result = classifier.classify("How does SiteRules work?")
        assert result.request_type == "explain"
        assert result.confidence >= 0.95

    def test_explain_what_is(self, classifier):
        result = classifier.classify("What is SiteCapsRegistry?")
        assert result.request_type == "explain"

    def test_explain_chinese(self, classifier):
        result = classifier.classify("SiteRules 是怎么工作的")
        assert result.request_type == "explain"

    def test_explain_chinese2(self, classifier):
        result = classifier.classify("解释一下这个设计模式")
        assert result.request_type == "explain"


class TestReviewOnlyClassification:
    def test_review_code(self, classifier):
        result = classifier.classify("Review this PR please")
        assert result.request_type == "review_only"
        assert result.confidence >= 0.95

    def test_review_chinese(self, classifier):
        result = classifier.classify("帮我看看这段代码")
        assert result.request_type == "review_only"

    def test_code_review(self, classifier):
        result = classifier.classify("code review my changes")
        assert result.request_type == "review_only"


class TestDesignOnlyClassification:
    def test_design_a(self, classifier):
        result = classifier.classify("Design a new payment system")
        assert result.request_type == "design_only"
        assert result.confidence >= 0.95

    def test_design_chinese(self, classifier):
        result = classifier.classify("设计一个消息推送系统")
        assert result.request_type == "design_only"

    def test_architecture(self, classifier):
        result = classifier.classify("architecture for offline sync")
        assert result.request_type == "design_only"


class TestCodeFallback:
    def test_code_request(self, classifier):
        result = classifier.classify("Implement a new feature")
        assert result.request_type == "code"

    def test_fix_bug(self, classifier):
        result = classifier.classify("Fix crash on login screen")
        assert result.request_type == "code"

    def test_chinese_implement(self, classifier):
        result = classifier.classify("实现一个登录页面")
        assert result.request_type == "code"

    def test_l0_auto_code(self, classifier):
        result = classifier.classify("change text color", level="L0")
        assert result.request_type == "code"
        assert result.confidence >= 0.9

    def test_ambiguous_fallback(self, classifier):
        result = classifier.classify("do something")
        assert result.request_type == "code"


class TestConfidenceThresholdFallback:
    def test_below_threshold_downgrades(self):
        with patch.object(RequestClassifier, "classify", return_value=ClassificationResult("explain", 0.5)):
            c = RequestClassifier()
            result = c.classify_with_fallback("something")
            assert result.request_type == "code"
            assert result.confidence == 1.0

    def test_above_threshold_keeps(self):
        with patch.object(RequestClassifier, "classify", return_value=ClassificationResult("explain", 0.95)):
            c = RequestClassifier()
            result = c.classify_with_fallback("something")
            assert result.request_type == "explain"


class TestConfigFlags:
    def test_explain_disabled(self, classifier):
        with patch("services.request_classifier.cfg_bool", return_value=False):
            result = classifier.classify("How does this work?")
            # explain disabled, but no code strong patterns, should fallback to code
            assert result.request_type == "code"

    def test_review_disabled(self, classifier):
        def fake_cfg(path, default):
            if "enable_review" in path:
                return False
            return default
        with patch("services.request_classifier.cfg_bool", side_effect=fake_cfg):
            result = classifier.classify("Review my code")
            assert result.request_type == "code"

    def test_design_disabled(self, classifier):
        def fake_cfg(path, default):
            if "enable_design" in path:
                return False
            return default
        with patch("services.request_classifier.cfg_bool", side_effect=fake_cfg):
            result = classifier.classify("Design a system")
            assert result.request_type == "code"
