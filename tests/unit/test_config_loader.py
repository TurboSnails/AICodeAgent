"""
config_loader.py 单元测试
覆盖：默认加载、local 覆盖、环境变量覆盖、类型转换、验证
"""
import os
import sys
from pathlib import Path

import pytest

from utils.config_loader import (
    Config, get_config, cfg, cfg_str, cfg_int, cfg_float, cfg_bool,
    _deep_get, _deep_set, _coerce,
)

class TestDeepGetSet:
    def test_deep_get_existing(self):
        d = {"a": {"b": {"c": 42}}}
        assert _deep_get(d, "a.b.c") == 42

    def test_deep_get_missing(self):
        d = {"a": {"b": {}}}
        assert _deep_get(d, "a.b.c", "default") == "default"

    def test_deep_get_non_dict_middle(self):
        d = {"a": "string"}
        assert _deep_get(d, "a.b", "default") == "default"

    def test_deep_set_nested(self):
        d = {}
        _deep_set(d, "x.y.z", 99)
        assert d == {"x": {"y": {"z": 99}}}

    def test_deep_set_override(self):
        d = {"x": {"y": {"z": 1}}}
        _deep_set(d, "x.y.z", 2)
        assert d["x"]["y"]["z"] == 2

class TestCoerce:
    def test_coerce_bool_true(self):
        assert _coerce("1", False) is True
        assert _coerce("true", False) is True
        assert _coerce("yes", False) is True
        assert _coerce("on", False) is True

    def test_coerce_bool_false(self):
        assert _coerce("0", True) is False
        assert _coerce("false", True) is False
        assert _coerce("no", True) is False
        assert _coerce("off", True) is False

    def test_coerce_int(self):
        assert _coerce("42", 0) == 42
        assert _coerce("10_000_000", 0) == 10000000

    def test_coerce_float(self):
        assert _coerce("3.14", 0.0) == 3.14

    def test_coerce_str(self):
        assert _coerce("hello", None) == "hello"

class TestConfigLoading:
    def test_singleton(self):
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2

    def test_default_values_present(self, monkeypatch):
        # 隔离可能已存在的环境变量
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        # 同时清理其他可能干扰默认值测试的环境变量
        for key in ["AGENT_DEBATE_TIMEOUT", "AGENT_SINGLE_TIMEOUT", "AGENT_TASK_TOTAL_TIMEOUT",
                    "AGENT_CLARIFICATION_TIMEOUT_HOURS", "AGENT_DEBATE_MAX_RETRY",
                    "AGENT_CONSENSUS_MAX_RETRY", "CODEX_REVIEW_MAX_RETRY",
                    "ACCEPTANCE_REVIEW_MAX_RETRY", "CLAUDE_CODE_MAX_RETRY",
                    "CLAUDE_RETRY_DELAY", "FIGMA_TOKEN", "FIGMA_FILE_KEY",
                    "FIGMA_RETRY_DELAY", "ASSET_HASH_SIMILARITY",
                    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "AGENT_API_KEY",
                    "AGENT_WEB_PORT", "AGENT_LOG_LEVEL", "AGENT_LOG_MAX_BYTES",
                    "AGENT_LOG_BACKUP_COUNT", "CRG_HTTP_URL", "CRG_REPO_ROOT",
                    "CRG_AUTO_START", "AGENT_SKIP_CLARIFICATION",
                    "ANDROID_HOME", "JAVA_HOME", "CLAUDE_CODE_AUTO_ALLOW_BASH"]:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("AGENT_DEBATE_TIMEOUT", raising=False)
        monkeypatch.delenv("CRG_AUTO_START", raising=False)
        Config._instance = None
        c = get_config()
        assert c.get_str("ai.claude_model") == ""  # 默认留空，由 Claude Code 自行决定
        assert c.get_int("timeouts.debate") == 1200
        assert c.get_float("retries.base_delay") == 3.0
        assert c.get_bool("crg.auto_start") is False
        Config._instance = None

    def test_env_override_int(self, monkeypatch):
        monkeypatch.setenv("AGENT_DEBATE_TIMEOUT", "999")
        Config._instance = None  # 重置单例
        c = get_config()
        assert c.get_int("timeouts.debate") == 999
        Config._instance = None  # 清理

    def test_env_override_bool(self, monkeypatch):
        monkeypatch.setenv("CRG_AUTO_START", "1")
        Config._instance = None
        c = get_config()
        assert c.get_bool("crg.auto_start") is True
        Config._instance = None

    def test_env_override_str(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
        Config._instance = None
        c = get_config()
        assert c.get_str("ai.claude_model") == "claude-opus-4-7"
        Config._instance = None

    def test_env_alias_anthropic_model(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        Config._instance = None
        c = get_config()
        assert c.get_str("ai.claude_model") == "claude-haiku-4-5"
        Config._instance = None

class TestConfigValidation:
    def test_validate_required_no_build_env(self, monkeypatch):
        monkeypatch.delenv("ANDROID_HOME", raising=False)
        monkeypatch.delenv("JAVA_HOME", raising=False)
        Config._instance = None
        c = get_config()
        missing = c.validate_required()
        # 默认值中 ANDROID_HOME / JAVA_HOME 为空
        assert any("ANDROID_HOME" in m for m in missing)
        assert any("JAVA_HOME" in m for m in missing)
        Config._instance = None