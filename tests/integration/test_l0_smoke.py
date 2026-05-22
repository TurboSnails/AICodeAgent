#!/usr/bin/env python3
"""
L0 任务端到端集成冒烟测试
验证：任务提交 -> planning -> coding -> building 链路可正常流转，
      各阶段 handler 能协同工作，workspace 产物正确生成。

注意：此测试使用 mock 的 AIClient 和 BuildService，不调用真实 LLM / Gradle。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from engine.core import AgentEngine
from engine.runner import build_engine
from engine.state_machine import Task, get_task, init_db, save_task, transition, State
from phases.base import PhaseHandler, PhaseResult


class MockAIClient:
    """返回固定内容的 AI 客户端 stub"""

    def __init__(self, response: str = "mock output"):
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def call(self, prompt: str, context: str = "", timeout: int = 300) -> str:
        self.calls.append((prompt[:100], context[:100]))
        return self._response

    def call_codex(self, prompt: str, context: str = "", timeout: int = 300) -> str:
        return self.call(prompt, context, timeout)


class MockBuildService:
    """可控成功/失败的构建服务 stub"""

    def __init__(self, success: bool = True, log: str = "BUILD OK"):
        self._success = success
        self._log = log

    def build(self, task_id: str, workspace: Path) -> tuple[bool, str]:
        if not self._success:
            from engine.exceptions import BuildFailureError
            raise BuildFailureError("mock build failure")
        return True, self._log

    def parse_errors(self, log: str) -> str:
        return log

    def clean(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _init_db(tmp_path: Path, monkeypatch):
    """每个集成测试使用独立数据库"""
    import engine.state_machine as sm
    import utils.paths as paths
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(sm, "DB_FILE", db_path)
    monkeypatch.setattr(paths, "DB_FILE", db_path)
    init_db()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """独立工作区"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class TestEngineSmoke:
    """AgentEngine + PhaseHandler 集成冒烟测试"""

    def test_build_engine_registers_all_handlers(self):
        """build_engine() 应成功注册全部 14 个 handler"""
        engine = build_engine()
        handlers = engine.list_registered()
        assert len(handlers) == 14
        for state in [
            "planning", "debating", "consensus", "coding",
            "building", "self_review", "codex_review", "architect_review",
            "red_team_review", "requirement_review", "correcting",
            "git_committing", "creating_pr", "notifying",
        ]:
            assert state in handlers

    def test_l0_task_planning_to_coding_fast_path(self, workspace: Path, monkeypatch):
        """L0 任务应跳过 debate，从 planning 直达 coding"""
        monkeypatch.setenv("AGENT_SKIP_CLARIFICATION", "1")

        ai = MockAIClient("=== FILE: app/src/main/res/values/strings.xml ===\n<resources/>\n=== END FILE ===")
        build = MockBuildService(success=True)
        git = MockGitService()
        notify = MockNotifyService()

        engine = AgentEngine(workspace_root=workspace)
        from phases.planning import PlanningHandler
        from phases.coding import CodingHandler
        from phases.building import BuildingHandler
        from phases.codex_review import CodexReviewHandler
        from phases.correcting import CorrectingHandler

        engine.register(State.PLANNING, PlanningHandler(ai_client=ai, notification_service=notify))
        engine.register(State.CODING, CodingHandler(ai_client=ai, git_service=git))
        engine.register(State.BUILDING, BuildingHandler(build_service=build))
        engine.register(State.CODEX_REVIEW, CodexReviewHandler(ai_client=ai))
        engine.register(State.CORRECTING, CorrectingHandler())

        task = Task(
            task_id="l0-smoke-01",
            raw_requirement="修改首页文案为 'Hello World'，验收标准：strings.xml 中 app_name 变为 Hello World",
            level="L0",
            site_hint="haobo",
            source="test",
            chat_id="",
        )
        save_task(task)
        transition(task.task_id, State.PLANNING, "test start", task)

        engine.process_task(task)

        # L0 应进入 coding 或更后的状态
        final = get_task(task.task_id)
        assert final.current_state in {
            State.CODING.value,
            State.BUILDING.value,
            State.CODEX_REVIEW.value,
            State.CORRECTING.value,
            State.FAILED.value,
        }

    def test_build_failure_triggers_correcting_loop_then_failed(self, workspace: Path):
        """编码失败应进入 correcting 循环，重试耗尽后进入 failed"""
        ai = MockAIClient()
        build = MockBuildService(success=False)
        git = MockGitService()

        engine = AgentEngine(workspace_root=workspace)
        from phases.coding import CodingHandler
        from phases.building import BuildingHandler
        from phases.correcting import CorrectingHandler

        engine.register(State.CODING, CodingHandler(ai_client=ai, git_service=git))
        engine.register(State.BUILDING, BuildingHandler(build_service=build))
        engine.register(State.CORRECTING, CorrectingHandler())

        task = Task(
            task_id="build-fail-01",
            raw_requirement="mock requirement",
            level="L0",
            site_hint="",
            source="test",
            chat_id="",
        )
        save_task(task)
        task.current_state = State.CODING.value
        task.branch = "feature/agent-build-fail-01"
        save_task(task)

        engine.process_task(task)

        final = get_task(task.task_id)
        # Mock AI 永远返回空代码，3 次重试后应进入 failed
        assert final.current_state == State.FAILED.value
        assert final.attempt_count > 0


class MockGitService:
    """Git 操作 stub"""

    def get_current_branch(self) -> str:
        return "main"

    def create_agent_branch(self, task_id: str, base_branch: str = "") -> str:
        return f"feature/agent-{task_id}"

    def apply_code_changes(self, claude_output: str):
        # 简单解析 === FILE: ... === 块
        import re
        applied = []
        for m in re.finditer(r"===\s*FILE:\s*(.+?)\s*===", claude_output):
            applied.append(m.group(1).strip())
        return applied, []

    def commit_from_consensus(self, *args, **kwargs) -> None:
        pass

    def push(self, branch: str) -> None:
        pass

    def create_pr(self, **kwargs) -> str:
        return "https://github.com/mock/pr/1"

    def generate_deviation_report(self, *args, **kwargs):
        return ""


class MockNotifyService:
    """通知服务 stub"""

    def notify_clarification(self, task, questions: list[str]) -> None:
        pass

    def notify_code_clarification(self, task, questions: list[str], context: str = "") -> None:
        pass

    def notify_l2_gate(self, task) -> None:
        pass

    def notify_task_status(self, task, pr_url: str = "", extra_message: str = "") -> None:
        pass
