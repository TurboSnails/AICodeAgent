#!/usr/bin/env python3
"""
配置验证器 — V4 重构
在 Agent 启动时执行一次配置完整性检查，提供清晰的错误提示。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from utils.config_loader import cfg_str, cfg_int, cfg_bool, Config
from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ConfigRule:
    """单个配置校验规则"""
    key: str
    required: bool = True
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    allowed_values: Optional[list[str]] = None
    description: str = ""


# 必要配置项清单
REQUIRED_RULES = [
    ConfigRule("webhook_secret", required=False, description="HTTP API 认证密钥（可选）"),
    ConfigRule("ai.claude_model", required=False, description="Claude 模型名称（可选，默认系统模型）"),
    ConfigRule("timeouts.claude_code", required=False, min_value=60, max_value=3600, description="Claude 调用超时（秒）"),
    ConfigRule("timeouts.build", required=False, min_value=60, max_value=3600, description="Gradle 构建超时（秒）"),
    ConfigRule("timeouts.debate", required=False, min_value=30, max_value=1800, description="Debate 超时（秒）"),
    ConfigRule("retries.coding", required=False, min_value=0, max_value=10, description="编码重试次数"),
    ConfigRule("retries.debate", required=False, min_value=0, max_value=5, description="辩论重试次数"),
    ConfigRule("retries.consensus", required=False, min_value=0, max_value=5, description="共识重试次数"),
    ConfigRule("retries.codex_review", required=False, min_value=0, max_value=5, description="Codex 审查重试次数"),
    ConfigRule("retries.acceptance_review", required=False, min_value=0, max_value=5, description="需求验收重试次数"),
]

# 警告级别配置项（有默认值但建议用户显式设置）
WARN_RULES = [
    ConfigRule("notifications.telegram.bot_token", required=False, description="Telegram Bot Token"),
    ConfigRule("notifications.telegram.chat_id", required=False, description="Telegram Chat ID"),
    ConfigRule("features.red_team_enabled", required=False, description="是否启用 Red Team 审查"),
    ConfigRule("features.red_team_for_levels", required=False, description="Red Team 审查的等级（默认 L2）"),
]


class ConfigValidator:
    """
    V4 配置验证器。

    职责：
    1. 检查必要配置项是否存在
    2. 校验数值范围
    3. 校验枚举值合法性
    4. 生成清晰的错误/警告报告
    """

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def validate(self) -> bool:
        """执行完整验证，返回是否通过（无 error）。"""
        self.errors.clear()
        self.warnings.clear()

        # 确保 Config 已加载
        Config().load()

        for rule in REQUIRED_RULES:
            self._check_rule(rule, is_required=True)

        for rule in WARN_RULES:
            self._check_rule(rule, is_required=False)

        # 额外检查：claude CLI 是否可用
        self._check_claude_cli()

        # 额外检查：gh CLI 是否可用（如果配置了 PR 创建）
        self._check_gh_cli()

        if self.errors:
            logger.error("Config validation FAILED with %d error(s):", len(self.errors))
            for e in self.errors:
                logger.error("  - %s", e)

        if self.warnings:
            logger.warning("Config validation has %d warning(s):", len(self.warnings))
            for w in self.warnings:
                logger.warning("  - %s", w)

        return len(self.errors) == 0

    def _check_rule(self, rule: ConfigRule, is_required: bool) -> None:
        """检查单个配置规则"""
        # 使用 config_loader 读取配置
        raw_value = cfg_str(rule.key, "")
        if not raw_value:
            if is_required and rule.required:
                self.errors.append(f"Missing required config: {rule.key} ({rule.description})")
            elif not is_required:
                self.warnings.append(f"Missing recommended config: {rule.key} ({rule.description})")
            return

        # 数值范围检查（尝试解析为 int）
        if rule.min_value is not None or rule.max_value is not None:
            try:
                int_val = int(raw_value)
                if rule.min_value is not None and int_val < rule.min_value:
                    self.errors.append(
                        f"Config {rule.key} = {int_val} is below minimum {rule.min_value}"
                    )
                if rule.max_value is not None and int_val > rule.max_value:
                    self.errors.append(
                        f"Config {rule.key} = {int_val} exceeds maximum {rule.max_value}"
                    )
            except ValueError:
                self.errors.append(f"Config {rule.key} = '{raw_value}' is not a valid integer")

        # 枚举值检查
        if rule.allowed_values is not None:
            if raw_value not in rule.allowed_values:
                self.errors.append(
                    f"Config {rule.key} = '{raw_value}' is not in allowed values: {rule.allowed_values}"
                )

    def _check_claude_cli(self) -> None:
        """检查 claude CLI 是否可用"""
        import shutil
        if not shutil.which("claude"):
            self.errors.append("claude CLI not found in PATH. Please install: npm install -g @anthropic-ai/claude-code")

    def _check_gh_cli(self) -> None:
        """检查 gh CLI 是否可用（仅警告，因为 PR 创建是可选功能）"""
        import shutil
        if not shutil.which("gh"):
            self.warnings.append("gh CLI not found in PATH. PR creation will be skipped.")

    def get_report(self) -> dict:
        """获取验证报告"""
        return {
            "passed": len(self.errors) == 0,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# 便捷函数
def validate_config() -> bool:
    """便捷函数：运行配置验证并返回是否通过"""
    return ConfigValidator().validate()


if __name__ == "__main__":
    import json
    validator = ConfigValidator()
    ok = validator.validate()
    print(json.dumps(validator.get_report(), indent=2, ensure_ascii=False))
    exit(0 if ok else 1)
