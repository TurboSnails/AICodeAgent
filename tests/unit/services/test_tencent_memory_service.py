#!/usr/bin/env python3
"""TencentDB Agent Memory 服务单元测试（mock HTTP）"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from services.tencent_memory_service import TencentMemoryService, _truncate


def _reset_config():
    from utils import config_loader
    config_loader.Config._instance = None
    import services.tencent_memory_service as mod
    mod._singleton = None


@pytest.fixture
def memory_cfg(monkeypatch):
    monkeypatch.setenv("MEMORY_TENCENTDB_ENABLED", "true")
    monkeypatch.setenv("MEMORY_TENCENTDB_GATEWAY_URL", "http://127.0.0.1:8420")
    _reset_config()
    yield
    _reset_config()


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("MEMORY_TENCENTDB_ENABLED", "false")
    _reset_config()
    svc = TencentMemoryService()
    assert not svc.enabled
    assert svc.recall("hello") == ""
    assert not svc.capture("u", "a")


def test_recall_success(memory_cfg):
    payload = json.dumps(
        {"context": "用户偏好 Kotlin", "strategy": "hybrid", "memory_count": 2}
    ).encode()

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/health"):
            return BytesIO(json.dumps({"status": "ok"}).encode())
        return BytesIO(payload)

    with patch("services.tencent_memory_service.urlopen", side_effect=fake_urlopen):
        svc = TencentMemoryService()
        assert svc.enabled
        assert svc.health_ok()
        ctx = svc.recall("wm 项目介绍")
        assert "Kotlin" in ctx


def test_capture_failure_non_fatal(memory_cfg):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    with patch("services.tencent_memory_service.urlopen", side_effect=boom):
        svc = TencentMemoryService()
        assert not svc.capture("user", "assistant")


def test_truncate():
    long = "x" * 100
    out = _truncate(long, 50)
    assert len(out) <= 50
    assert "truncated" in out
