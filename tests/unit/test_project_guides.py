"""项目指南与构建策略解析"""

from pathlib import Path

import pytest

from utils.project_guides import (
    resolve_build_policy,
    write_build_policy_files,
    read_build_policy,
    load_project_guides_text,
)


def test_load_guides_prefers_claude(tmp_path, monkeypatch):
    root = tmp_path / "wm"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# C\n```bash\n./gradlew app:assembleDebug\n```", encoding="utf-8")
    (root / "AGENTS.md").write_text("# A\n```bash\n./gradlew test\n```", encoding="utf-8")
    monkeypatch.setattr("utils.project_guides.PROJECT_ROOT", root, raising=False)

    text, source = load_project_guides_text()
    assert "CLAUDE.md" in text
    assert "AGENTS.md" in text
    assert source == "CLAUDE.md"


def test_resolve_assemble_only_for_compile_acceptance(tmp_path, monkeypatch):
    root = tmp_path / "wm"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "## Build\n```bash\n./gradlew app:assembleDebug\n./gradlew test\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("utils.project_guides.PROJECT_ROOT", root, raising=False)
    monkeypatch.setattr("utils.project_guides.cfg_bool", lambda _k, default=True: default, raising=False)

    policy = resolve_build_policy("验收标准就是编译正常", level="L0")
    assert policy.assemble_only is True
    assert policy.gradle_tasks == ["app:assembleDebug"]
    assert "assembleDebug" in policy.verify_command


def test_resolve_includes_tests_when_required(tmp_path, monkeypatch):
    root = tmp_path / "wm"
    root.mkdir()
    (root / "AGENTS.md").write_text("```bash\n./gradlew app:assembleDebug\n```", encoding="utf-8")
    monkeypatch.setattr("utils.project_guides.PROJECT_ROOT", root, raising=False)
    monkeypatch.setattr("utils.project_guides.cfg_bool", lambda _k, default=True: False, raising=False)

    policy = resolve_build_policy("请跑 testDebugUnitTest 验证", level="L1")
    assert policy.assemble_only is False
    assert "testDebugUnitTest" in policy.gradle_tasks


def test_write_and_read_policy_roundtrip(tmp_path, monkeypatch):
    root = tmp_path / "wm"
    root.mkdir()
    (root / "CLAUDE.md").write_text("```bash\n./gradlew app:assembleDebug\n```", encoding="utf-8")
    monkeypatch.setattr("utils.project_guides.PROJECT_ROOT", root, raising=False)
    monkeypatch.setattr("utils.project_guides.cfg_bool", lambda _k, default=True: True, raising=False)

    ws = tmp_path / "ws"
    policy = resolve_build_policy("编译通过即可")
    write_build_policy_files(ws, policy)
    loaded = read_build_policy(ws)
    assert loaded is not None
    assert loaded.assemble_only is True
    assert (ws / "project_guides.md").exists()
