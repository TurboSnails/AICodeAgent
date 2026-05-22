"""
统一配置加载器
三层优先级：环境变量 > config/local.yaml > config/default.yaml
支持点号路径访问：config.get("ai.claude_model")
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.yaml"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "local.yaml"

# 环境变量到配置路径的映射（支持覆盖嵌套配置）
_ENV_MAP: dict[str, str] = {
    "CLAUDE_MODEL": "ai.claude_model",
    "ANTHROPIC_MODEL": "ai.claude_model",  # 别名
    "CODEX_CMD": "ai.codex_cmd",
    "CODEX_REVIEW_TIMEOUT": "ai.codex_timeout",
    "AGENT_LLM_TIMEOUT": "timeouts.llm",
    "AGENT_DEBATE_TIMEOUT": "timeouts.debate",
    "AGENT_SINGLE_TIMEOUT": "timeouts.agent_single",
    "AGENT_CLAUDE_CODE_TIMEOUT": "timeouts.claude_code",
    "AGENT_CLAUDE_CODE_L0_TIMEOUT": "timeouts.claude_code_l0",
    "AGENT_TASK_TOTAL_TIMEOUT": "timeouts.task_total",
    "AGENT_CLARIFICATION_TIMEOUT_HOURS": "timeouts.clarification_hours",
    "AGENT_DEBATE_MAX_RETRY": "retries.debate",
    "AGENT_CONSENSUS_MAX_RETRY": "retries.consensus",
    "CODEX_REVIEW_MAX_RETRY": "retries.codex_review",
    "ACCEPTANCE_REVIEW_MAX_RETRY": "retries.acceptance_review",
    "CLAUDE_CODE_MAX_RETRY": "retries.claude_code",
    "CLAUDE_RETRY_DELAY": "retries.base_delay",
    "FIGMA_TOKEN": "figma.token",
    "FIGMA_FILE_KEY": "figma.file_key",
    "FIGMA_RETRY_DELAY": "figma.retry_delay",
    "ASSET_HASH_SIMILARITY": "figma.hash_similarity",
    "TELEGRAM_BOT_TOKEN": "notifications.telegram.bot_token",
    "TELEGRAM_CHAT_ID": "notifications.telegram.chat_id",
    "AGENT_API_KEY": "gateway.api_key",
    "AGENT_WEB_PORT": "gateway.web_port",
    "AGENT_LOG_LEVEL": "logging.level",
    "AGENT_LOG_MAX_BYTES": "logging.max_bytes",
    "AGENT_LOG_BACKUP_COUNT": "logging.backup_count",
    "CRG_HTTP_URL": "crg.http_url",
    "CRG_REPO_ROOT": "crg.repo_root",
    "CRG_AUTO_START": "crg.auto_start",
    "MEMORY_TENCENTDB_ENABLED": "memory.tencentdb.enabled",
    "MEMORY_TENCENTDB_GATEWAY_URL": "memory.tencentdb.gateway_url",
    "MEMORY_TENCENTDB_SESSION_KEY": "memory.tencentdb.session_key",
    "AGENT_SKIP_CLARIFICATION": "features.skip_clarification",
    "SELF_REVIEW_ENABLED": "features.self_review_enabled",
    "SELF_REVIEW_THRESHOLD": "features.self_review_threshold",
    "SELF_REVIEW_MAX_RETRY": "features.self_review_max_retry",
    "ARCHITECT_REVIEW_ENABLED": "features.architect_review_enabled",
    "ARCHITECT_FOR_LEVELS": "features.architect_for_levels",
    "ARCHITECT_MAX_RETRY": "features.architect_max_retry",
    "ANDROID_HOME": "build.android_home",
    "JAVA_HOME": "build.java_home",
    "CLAUDE_CODE_AUTO_ALLOW_BASH": "build.auto_allow_bash",
}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _deep_get(d: dict, path: str, default: Any = None) -> Any:
    keys = path.split(".")
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
        if d is None:
            return default
    return d


def _deep_set(d: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _coerce(value: str, target: Any) -> Any:
    """根据目标值的类型进行类型转换"""
    if target is None:
        # 纯字符串
        return value
    if isinstance(target, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(target, int):
        try:
            return int(value.replace("_", ""))
        except ValueError:
            return target
    if isinstance(target, float):
        try:
            return float(value)
        except ValueError:
            return target
    return value


class Config:
    """统一配置对象（单例）"""

    _instance: Optional["Config"] = None

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data: dict = {}
            cls._instance._loaded = False
        return cls._instance

    def load(self) -> "Config":
        if self._loaded:
            return self

        # 1. 加载 default.yaml
        self._data = _load_yaml(DEFAULT_CONFIG)

        # 2. 加载 local.yaml 覆盖
        local = _load_yaml(LOCAL_CONFIG)
        self._merge(local)

        # 3. 环境变量覆盖（带类型推断）
        for env_key, config_path in _ENV_MAP.items():
            env_val = os.environ.get(env_key)
            if env_val:
                current = _deep_get(self._data, config_path)
                coerced = _coerce(env_val, current)
                _deep_set(self._data, config_path, coerced)

        self._loaded = True
        return self

    def _merge(self, other: dict, base: Optional[dict] = None) -> None:
        base = base if base is not None else self._data
        for k, v in other.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                self._merge(v, base[k])
            else:
                base[k] = v

    def get(self, path: str, default: Any = None) -> Any:
        self.load()
        return _deep_get(self._data, path, default)

    def get_str(self, path: str, default: str = "") -> str:
        val = self.get(path, default)
        return str(val) if val is not None else default

    def get_int(self, path: str, default: int = 0) -> int:
        val = self.get(path, default)
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def get_float(self, path: str, default: float = 0.0) -> float:
        val = self.get(path, default)
        if isinstance(val, (int, float)):
            return float(val)
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def get_bool(self, path: str, default: bool = False) -> bool:
        val = self.get(path, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
        return bool(val)

    def all(self) -> dict:
        self.load()
        return self._data.copy()

    def validate_required(self) -> list[str]:
        """
        返回未配置的关键必填项列表（警告级别，不阻塞启动）。
        实际阻塞逻辑由调用方决定。
        """
        missing: list[str] = []
        # 构建环境（编码/构建必需）
        if not self.get_str("build.android_home"):
            missing.append("ANDROID_HOME (build.android_home)")
        if not self.get_str("build.java_home"):
            missing.append("JAVA_HOME (build.java_home)")
        return missing


def get_config() -> Config:
    """获取已加载的配置单例"""
    return Config().load()


# 模块级便捷函数（向后兼容风格）
def cfg(path: str, default: Any = None) -> Any:
    return get_config().get(path, default)


def cfg_str(path: str, default: str = "") -> str:
    return get_config().get_str(path, default)


def cfg_int(path: str, default: int = 0) -> int:
    return get_config().get_int(path, default)


def cfg_float(path: str, default: float = 0.0) -> float:
    return get_config().get_float(path, default)


def cfg_bool(path: str, default: bool = False) -> bool:
    return get_config().get_bool(path, default)
