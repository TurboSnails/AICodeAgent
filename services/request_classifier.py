#!/usr/bin/env python3
"""
请求类型分类器 — V4 路由新增
基于规则的混合分类器（可扩展 LLM 辅助）。
职责：
1. 根据需求文本判定请求类型
2. 输出置信度，低置信度降级为 CODE_REQUEST
3. 支持配置开关各类型
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.config_loader import cfg_bool, cfg_float
from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    """分类结果"""
    request_type: str
    confidence: float


class RequestClassifier:
    """
    请求类型分类器 — 规则为主，可扩展 LLM 为辅。

    分类逻辑：
    1. 先进行规则匹配（正则关键词），命中则高置信度返回
    2. 规则未命中则降级为 CODE_REQUEST（兜底安全路径）
    3. 配置关闭的类型直接降级
    """

    # Explain 模式触发词
    EXPLAIN_PATTERNS = [
        r"怎么工作", r"怎么.?实现", r"怎么.?运行", r"怎么.?使用",
        r"解释一下", r"解释.*原理", r"解释.*机制",
        r"是什么", r"什么是", r"啥是",
        r"原理", r"机制", r"逻辑", r"流程",
        r"how\s+does", r"how\s+is", r"how\s+to\s+use",
        r"what\s+is", r"what\s+are", r"what\s+does",
        r"explain", r"explains?\s+how",
        r"tell\s+me\s+about", r"describe",
    ]

    # Review-only 模式触发词
    REVIEW_PATTERNS = [
        r"review", r"review\s+this", r"review\s+my", r"review\s+the",
        r"code\s+review", r"pr\s+review",
        r"检查.*代码", r"帮我看看", r"帮我检查",
        r"看看.*这段", r"看看.*这个",
        r"audit", r"inspect",
    ]

    # Design-only 模式触发词
    DESIGN_PATTERNS = [
        r"设计一个", r"设计方案", r"设计.*架构", r"架构设计",
        r"给我个方案", r"给个方案", r"设计方案",
        r"design\s+a", r"design\s+an",
        r"architecture\s+for", r"architecture\s+of",
        r"propose\s+a\s+design", r"propose\s+an\s+architecture",
        r"system\s+design", r"high.level\s+design",
    ]

    # Code 模式强指示词（用于排除其他类型）
    CODE_STRONG_PATTERNS = [
        r"实现", r"添加", r"创建", r"新建",
        r"修复", r"fix", r"bugfix",
        r"重构", r"migrate", r"迁移",
        r"修改", r"更新", r"删除",
        r"add\s+", r"create\s+", r"implement\s+",
        r"fix\s+", r"refactor\s+", r"update\s+",
    ]

    def __init__(self):
        self._confidence_threshold = cfg_float("routing.confidence_threshold", 0.7)

    def classify(self, requirement: str, level: str = "auto") -> ClassificationResult:
        """
        根据需求文本和等级判定请求类型。

        :param requirement: 原始需求文本
        :param level: 任务等级（L0/L1/L2/auto）
        :return: ClassificationResult(request_type, confidence)
        """
        req_lower = requirement.lower().strip()
        if not req_lower:
            return ClassificationResult("code", 1.0)

        # 检查各类型是否启用
        explain_enabled = cfg_bool("routing.enable_explain", True)
        review_enabled = cfg_bool("routing.enable_review_only", True)
        design_enabled = cfg_bool("routing.enable_design_only", True)

        # 如果包含强 Code 指示词，优先判定为 code（避免误判）
        has_code_strong = any(re.search(p, req_lower) for p in self.CODE_STRONG_PATTERNS)

        # 1. Explain 分类
        if explain_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.EXPLAIN_PATTERNS):
                return ClassificationResult("explain", 0.95)

        # 2. Review-only 分类
        if review_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.REVIEW_PATTERNS):
                return ClassificationResult("review_only", 0.95)

        # 3. Design-only 分类
        if design_enabled and not has_code_strong:
            if any(re.search(p, req_lower) for p in self.DESIGN_PATTERNS):
                return ClassificationResult("design_only", 0.95)

        # 4. 兜底：CODE_REQUEST
        # L0 小改明确为 code
        if level == "L0":
            return ClassificationResult("code", 0.9)

        return ClassificationResult("code", 0.8)

    def classify_with_fallback(self, requirement: str, level: str = "auto") -> ClassificationResult:
        """
        带降级兜底的分类：置信度低于阈值时强制降级为 CODE_REQUEST。
        """
        result = self.classify(requirement, level)
        if result.confidence < self._confidence_threshold:
            logger.info(
                "Classification confidence %.2f below threshold %.2f, fallback to code",
                result.confidence, self._confidence_threshold,
            )
            return ClassificationResult("code", 1.0)
        return result
