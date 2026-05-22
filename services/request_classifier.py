#!/usr/bin/env python3
"""
请求类型分类器 — V4 路由
支持 rule / llm / hybrid 三种模式，LLM 失败时回退规则。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from utils.config_loader import cfg_bool, cfg_float, cfg_int, cfg_str
from utils.logging_config import get_logger

logger = get_logger(__name__)

VALID_REQUEST_TYPES = frozenset({"code", "explain", "review_only", "design_only"})


@dataclass(frozen=True)
class ClassificationResult:
    """分类结果"""
    request_type: str
    confidence: float
    source: str = "rule"  # rule | llm


class RequestClassifier:
    """
    请求类型分类器。

    routing.classifier:
      - rule: 仅关键词规则
      - llm: 优先 LLM，失败则规则
      - hybrid: 同 llm（显式语义：AI 主判 + 规则兜底）
    """

    EXPLAIN_PATTERNS = [
        r"怎么工作", r"怎么.?实现", r"怎么.?运行", r"怎么.?使用",
        r"解释一下", r"解释.*原理", r"解释.*机制",
        r"介绍", r"介绍下", r"介绍一下",
        r"是什么", r"什么是", r"啥是", r"是啥",
        r"有啥功能", r"做什么的", r"干什么的", r"干什么用",
        r"项目.*功能", r"当前.*项目", r"这个.*项目",
        r"概况", r"概述", r"整体.*架构",
        r"原理", r"机制", r"逻辑", r"流程",
        r"how\s+does", r"how\s+is", r"how\s+to\s+use",
        r"what\s+is", r"what\s+are", r"what\s+does",
        r"explain", r"explains?\s+how",
        r"tell\s+me\s+about", r"describe", r"overview",
    ]

    REVIEW_PATTERNS = [
        r"review", r"review\s+this", r"review\s+my", r"review\s+the",
        r"code\s+review", r"pr\s+review",
        r"检查.*代码", r"帮我看看", r"帮我检查",
        r"看看.*这段", r"看看.*这个",
        r"audit", r"inspect",
    ]

    DESIGN_PATTERNS = [
        r"设计一个", r"设计方案", r"设计.*架构", r"架构设计",
        r"给我个方案", r"给个方案", r"设计方案",
        r"design\s+a", r"design\s+an",
        r"architecture\s+for", r"architecture\s+of",
        r"propose\s+a\s+design", r"propose\s+an\s+architecture",
        r"system\s+design", r"high.level\s+design",
    ]

    CODE_STRONG_PATTERNS = [
        r"实现", r"添加", r"创建", r"新建",
        r"修复", r"fix", r"bugfix",
        r"重构", r"migrate", r"迁移",
        r"修改", r"更新", r"删除",
        r"add\s+", r"create\s+", r"implement\s+",
        r"fix\s+", r"refactor\s+", r"update\s+",
    ]

    # UI/组件具体问题描述 → 明确要动代码，hybrid 下跳过 LLM 分类
    CODE_UI_BUG_PATTERNS = [
        r"vippager", r"vip\s*pager", r"vipcard",
        r"渐变", r"颜色", r"配色", r"滑动", r"pager",
        r"不正确", r"不对", r"少了", r"少了一", r"缺少",
        r"优化点", r"需要优化",
    ]

    _CLASSIFY_PROMPT = """你是 Android 多站点代码仓库的任务路由器。根据用户原始需求，判断应走哪条流水线。

类型说明（四选一）：
- explain：问答、介绍、原理说明、项目/模块概览；不要求改代码、不写 PR
- review_only：审查已有代码/PR/改动，只输出审查意见，不实现功能
- design_only：只要架构/方案/设计文档，不要求落地编码
- code：实现、修复、重构、改 UI/配置、加功能、删改文件等需要动仓库的任务

判定原则：
1. 用户只要「了解/介绍/解释/怎么回事」→ explain，即使提到项目名或功能名
2. 用户明确要改代码、修 bug、加页面 → code；若同时「顺便解释原理」仍以 code 为主
3. 仅「帮我看看代码/审查 PR」且无实现诉求 → review_only
4. 仅「出个方案/架构设计」且未要求实现 → design_only
5. 含糊时倾向 code（安全路径），但纯信息类问题不要误判为 code

只输出一行 JSON，不要 markdown，不要其它文字：
{{"request_type":"code|explain|review_only|design_only","confidence":0.0到1.0,"reason":"一句话"}}

用户需求：
{requirement}
"""

    def __init__(self, ai_client=None):
        self._ai = ai_client
        self._confidence_threshold = cfg_float("routing.confidence_threshold", 0.7)

    def classify(self, requirement: str, level: str = "auto") -> ClassificationResult:
        req = (requirement or "").strip()
        if not req:
            return ClassificationResult("code", 1.0, "rule")

        if not cfg_bool("routing.enabled", True):
            return ClassificationResult("code", 1.0, "rule")

        mode = cfg_str("routing.classifier", "hybrid").strip().lower()
        if mode in ("llm", "hybrid") and cfg_bool("routing.rules_first", True):
            fast = self._classify_by_rules_fast(req, level)
            if fast is not None:
                return self._apply_type_gates(fast)

        if mode in ("llm", "hybrid"):
            llm_result = self._classify_by_llm(req, level)
            if llm_result is not None:
                gated = self._apply_type_gates(llm_result)
                return gated

            if mode == "llm":
                logger.warning("LLM routing failed, falling back to rules")
            else:
                logger.info("Hybrid routing: LLM unavailable, using rules")

        result = self._classify_by_rules(req, level)
        return self._apply_type_gates(result)

    def classify_with_fallback(self, requirement: str, level: str = "auto") -> ClassificationResult:
        """置信度低于阈值时降级为 code。"""
        result = self.classify(requirement, level)
        if result.confidence < self._confidence_threshold:
            logger.info(
                "Classification confidence %.2f below threshold %.2f, fallback to code",
                result.confidence, self._confidence_threshold,
            )
            return ClassificationResult("code", 1.0, result.source)
        return result

    def _classify_by_llm(self, requirement: str, level: str) -> Optional[ClassificationResult]:
        if self._ai is None:
            logger.debug("No AI client for routing, skip LLM classify")
            return None

        prompt = self._CLASSIFY_PROMPT.format(requirement=requirement[:4000])
        timeout = cfg_int("routing.classification_timeout", 500)

        try:
            raw = self._ai.call(prompt, context="", timeout=timeout, headless=True)
        except Exception as e:
            logger.warning("LLM classify call failed: %s", e)
            return None

        parsed = self._parse_llm_response(raw)
        if parsed is None:
            logger.warning("LLM classify parse failed, raw=%s", (raw or "")[:200])
            return None

        request_type, confidence, _reason = parsed
        logger.info(
            "LLM classified as %s (confidence=%.2f): %s",
            request_type, confidence, _reason[:80] if _reason else "",
        )
        return ClassificationResult(request_type, confidence, "llm")

    @staticmethod
    def _parse_llm_response(raw: str) -> Optional[tuple[str, float, str]]:
        if not raw or not raw.strip():
            return None

        text = raw.strip()
        # ```json ... ``` 或裸 JSON
        blocks = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE)
        candidates = blocks if blocks else [text]
        for chunk in candidates:
            chunk = chunk.strip()
            if not chunk:
                continue
            obj = RequestClassifier._try_parse_json_object(chunk)
            if obj:
                return obj
        return None

    @staticmethod
    def _try_parse_json_object(text: str) -> Optional[tuple[str, float, str]]:
        # 从文本中提取第一个 {...}
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    snippet = text[start : i + 1]
                    try:
                        data = json.loads(snippet)
                    except json.JSONDecodeError:
                        return None
                    if not isinstance(data, dict):
                        return None
                    rt = str(data.get("request_type", "")).strip().lower()
                    if rt not in VALID_REQUEST_TYPES:
                        return None
                    try:
                        conf = float(data.get("confidence", 0.8))
                    except (TypeError, ValueError):
                        conf = 0.8
                    conf = max(0.0, min(1.0, conf))
                    reason = str(data.get("reason", "")).strip()
                    return rt, conf, reason
        return None

    def _classify_by_rules_fast(self, requirement: str, level: str) -> Optional[ClassificationResult]:
        """高置信规则命中时跳过 LLM（避免 Kimi 分类 60s+ 阻塞 planning）。"""
        req_lower = requirement.lower().strip()
        if any(re.search(p, req_lower, re.I) for p in self.CODE_UI_BUG_PATTERNS):
            logger.info("Rules-first: UI/component bug pattern -> code (skip LLM)")
            return ClassificationResult("code", 0.96, "rule-fast")
        if any(re.search(p, req_lower) for p in self.CODE_STRONG_PATTERNS):
            logger.info("Rules-first: code-strong pattern -> code (skip LLM)")
            return ClassificationResult("code", 0.96, "rule-fast")
        if level == "L0":
            logger.info("Rules-first: L0 task -> code (skip LLM)")
            return ClassificationResult("code", 0.92, "rule-fast")
        return None

    def _classify_by_rules(self, requirement: str, level: str) -> ClassificationResult:
        req_lower = requirement.lower().strip()

        explain_enabled = cfg_bool("routing.enable_explain", True)
        review_enabled = cfg_bool("routing.enable_review_only", True)
        design_enabled = cfg_bool("routing.enable_design_only", True)

        has_code_strong = any(re.search(p, req_lower) for p in self.CODE_STRONG_PATTERNS)
        has_ui_bug = any(re.search(p, req_lower, re.I) for p in self.CODE_UI_BUG_PATTERNS)
        if has_code_strong or has_ui_bug:
            return ClassificationResult("code", 0.95, "rule")

        if explain_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.EXPLAIN_PATTERNS):
                return ClassificationResult("explain", 0.95, "rule")

        if review_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.REVIEW_PATTERNS):
                return ClassificationResult("review_only", 0.95, "rule")

        if design_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.DESIGN_PATTERNS):
                return ClassificationResult("design_only", 0.95, "rule")

        if level == "L0":
            return ClassificationResult("code", 0.9, "rule")

        return ClassificationResult("code", 0.8, "rule")

    @staticmethod
    def _apply_type_gates(result: ClassificationResult) -> ClassificationResult:
        """配置关闭的类型降级为 code。"""
        rt = result.request_type
        if rt == "explain" and not cfg_bool("routing.enable_explain", True):
            return ClassificationResult("code", result.confidence, result.source)
        if rt == "review_only" and not cfg_bool("routing.enable_review_only", True):
            return ClassificationResult("code", result.confidence, result.source)
        if rt == "design_only" and not cfg_bool("routing.enable_design_only", True):
            return ClassificationResult("code", result.confidence, result.source)
        return result
