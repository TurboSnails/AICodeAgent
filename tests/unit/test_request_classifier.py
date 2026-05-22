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


class TestRulesFirstFastPath:
    @patch("services.request_classifier.cfg_bool", side_effect=lambda k, d=False: k == "routing.rules_first" if k == "routing.rules_first" else (k == "routing.enabled" or d))
    @patch("services.request_classifier.cfg_str", return_value="hybrid")
    def test_vippager_skips_llm(self, _cfg_str, _cfg_bool):
        ai = MagicMock()
        clf = RequestClassifier(ai_client=ai)
        req = "帮我看下 VIPPager 颜色渐变从第二个开始不对"
        result = clf.classify(req, "L0")
        assert result.request_type == "code"
        assert result.source == "rule-fast"
        ai.call.assert_not_called()


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

    def test_explain_project_intro(self, classifier):
        result = classifier.classify("请介绍下当前这个wm项目 功能是啥")
        assert result.request_type == "explain"
        assert result.confidence >= 0.95

    def test_explain_introduce(self, classifier):
        result = classifier.classify("介绍一下这个项目是做什么的")
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


class TestLlmRouting:
    def test_llm_explain(self):
        ai = MagicMock()
        ai.call.return_value = (
            '{"request_type":"explain","confidence":0.92,"reason":"项目介绍"}'
        )
        with patch("services.request_classifier.cfg_str", return_value="llm"):
            c = RequestClassifier(ai_client=ai)
            result = c.classify("随便一句模糊话")
        assert result.request_type == "explain"
        assert result.source == "llm"
        ai.call.assert_called_once()

    def test_llm_parse_codeblock(self):
        ai = MagicMock()
        ai.call.return_value = '```json\n{"request_type":"code","confidence":0.88,"reason":"实现"}\n```'
        with patch("services.request_classifier.cfg_str", return_value="hybrid"):
            c = RequestClassifier(ai_client=ai)
            result = c.classify("给登录页加个按钮")
        assert result.request_type == "code"
        assert result.source == "llm"

    def test_llm_fail_falls_back_to_rules(self):
        ai = MagicMock()
        ai.call.side_effect = RuntimeError("timeout")
        with patch("services.request_classifier.cfg_str", return_value="hybrid"):
            c = RequestClassifier(ai_client=ai)
            result = c.classify("请介绍下当前这个wm项目 功能是啥")
        assert result.request_type == "explain"
        assert result.source == "rule"

    def test_llm_invalid_json_falls_back(self):
        ai = MagicMock()
        ai.call.return_value = "I think this is explain type"
        with patch("services.request_classifier.cfg_str", return_value="llm"):
            c = RequestClassifier(ai_client=ai)
            result = c.classify("How does SiteRules work?")
        assert result.request_type == "explain"
        assert result.source == "rule"


class TestParseLlmResponse:
    def test_bare_json(self):
        parsed = RequestClassifier._parse_llm_response(
            '{"request_type":"review_only","confidence":0.9,"reason":"PR"}'
        )
        assert parsed == ("review_only", 0.9, "PR")

    def test_invalid_type(self):
        assert RequestClassifier._parse_llm_response('{"request_type":"magic"}') is None
