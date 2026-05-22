"""pytest 共享配置与 fixture"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """为每个测试提供独立的临时 SQLite 数据库"""
    return tmp_path / "test_agent.db"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """隔离环境变量，防止测试污染宿主环境"""
    # 关键变量置空或设默认值
    monkeypatch.setenv("AGENT_API_KEY", "test-api-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("FIGMA_TOKEN", "")
    monkeypatch.setenv("AGENT_DEBATE_TIMEOUT", "600")
    monkeypatch.setenv("AGENT_CONSENSUS_MAX_RETRY", "2")
    monkeypatch.setenv("CODEX_REVIEW_MAX_RETRY", "2")
    monkeypatch.setenv("ACCEPTANCE_REVIEW_MAX_RETRY", "2")
    monkeypatch.setenv("AGENT_CLARIFICATION_TIMEOUT_HOURS", "48")
    monkeypatch.setenv("AGENT_TASK_TOTAL_TIMEOUT", "7200")
    monkeypatch.setenv("CODEX_REVIEW_TIMEOUT", "900")
    monkeypatch.setenv("CODEX_CMD", "")
    monkeypatch.setenv("AGENT_SKIP_CLARIFICATION", "0")
    monkeypatch.setenv("CRG_AUTO_START", "0")
    monkeypatch.setenv("CRG_HTTP_URL", "http://127.0.0.1:5555")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    monkeypatch.setenv("FIGMA_FILE_KEY", "")
    monkeypatch.setenv("AGENT_WEB_PORT", "6789")
    monkeypatch.setenv("AGENT_LOG_LEVEL", "INFO")
    monkeypatch.setenv("AGENT_LOG_MAX_BYTES", "10000000")
    monkeypatch.setenv("AGENT_LOG_BACKUP_COUNT", "5")


@pytest.fixture
def mock_project_root(tmp_path: Path) -> Path:
    """模拟 Android 项目根目录结构"""
    root = tmp_path / "android_project"
    # buildSrc / Configs.kt
    buildSrc = root / "buildSrc" / "src" / "main" / "kotlin"
    buildSrc.mkdir(parents=True)
    (buildSrc / "Configs.kt").write_text('val site: Site = HaoboDebug\n', encoding="utf-8")
    # app/src/main/res/values/strings.xml
    (root / "app" / "src" / "main" / "res" / "values").mkdir(parents=True)
    (root / "app" / "src" / "main" / "res" / "values" / "strings.xml").write_text(
        '<resources><string name="app_name">Test</string></resources>', encoding="utf-8"
    )
    return root
