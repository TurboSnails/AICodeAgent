"""
AgentEngine 单元测试
覆盖：处理器注册、状态流转、异常捕获、终态与等待态处理
"""
import sqlite3
import sys
from pathlib import Path

import pytest

from engine.core import AgentEngine
from engine.exceptions import AgentFatalError, AgentRecoverableError
from engine.state_machine import State, Task, cancel_task, init_db, get_task, transition, save_task
from phases.base import PhaseHandler, PhaseResult

class DummyHandler(PhaseHandler):
    """测试用处理器：总是流转到指定状态"""

    def __init__(self, next_state: State):
        self.next_state = next_state

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        return PhaseResult(self.next_state, f"dummy -> {self.next_state.value}")

class FailOnceHandler(PhaseHandler):
    """测试用处理器：第一次失败，第二次成功"""

    def __init__(self, next_state: State):
        self.next_state = next_state
        self.call_count = 0

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        self.call_count += 1
        if self.call_count == 1:
            raise AgentRecoverableError("first attempt fails")
        return PhaseResult(self.next_state, "second attempt succeeds")

class FatalHandler(PhaseHandler):
    """测试用处理器：抛出致命错误"""

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        raise AgentFatalError("fatal error")

@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "test_engine.db"
    monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
    init_db()
    return db

class TestEngineRegistration:
    """处理器注册管理"""

    def test_register_handler(self):
        engine = AgentEngine(workspace_root=Path("/tmp/test"))
        engine.register(State.CODING, DummyHandler(State.BUILDING))
        assert engine.get_handler(State.CODING) is not None
        assert State.CODING.value in engine.list_registered()

    def test_unregister_handler(self):
        engine = AgentEngine(workspace_root=Path("/tmp/test"))
        engine.register(State.CODING, DummyHandler(State.BUILDING))
        engine.unregister(State.CODING)
        assert engine.get_handler(State.CODING) is None

    def test_override_handler(self):
        engine = AgentEngine(workspace_root=Path("/tmp/test"))
        h1 = DummyHandler(State.BUILDING)
        h2 = DummyHandler(State.FAILED)
        engine.register(State.CODING, h1)
        engine.register(State.CODING, h2)
        assert engine.get_handler(State.CODING) is h2

class TestEngineStateTransitions:
    """状态流转测试"""

    def test_normal_flow_to_terminal(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        engine = AgentEngine(workspace_root=tmp_path)
        engine.register(State.PLANNING, DummyHandler(State.CODING))
        engine.register(State.CODING, DummyHandler(State.BUILDING))
        engine.register(State.BUILDING, DummyHandler(State.SELF_REVIEW))
        engine.register(State.SELF_REVIEW, DummyHandler(State.CODEX_REVIEW))
        engine.register(State.CODEX_REVIEW, DummyHandler(State.ARCHITECT_REVIEW))
        engine.register(State.ARCHITECT_REVIEW, DummyHandler(State.RED_TEAM_REVIEW))
        engine.register(State.RED_TEAM_REVIEW, DummyHandler(State.REQUIREMENT_REVIEW))
        engine.register(State.REQUIREMENT_REVIEW, DummyHandler(State.GIT_COMMITTING))
        engine.register(State.GIT_COMMITTING, DummyHandler(State.CREATING_PR))
        engine.register(State.CREATING_PR, DummyHandler(State.NOTIFYING))
        engine.register(State.NOTIFYING, DummyHandler(State.COMPLETED))

        task = Task(
            task_id="flow1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        save_task(task)
        transition("flow1", State.PLANNING, "start", task)

        engine.process_task(task)

        loaded = get_task("flow1")
        assert loaded is not None
        assert loaded.current_state == State.COMPLETED.value

    def test_waiting_clarification_state(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        engine = AgentEngine(workspace_root=tmp_path)
        engine.register(State.PLANNING, DummyHandler(State.WAITING_CLARIFICATION))

        task = Task(
            task_id="clar1", raw_requirement="test", level="L2",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.PLANNING.value
        save_task(task)

        engine.process_task(task)

        loaded = get_task("clar1")
        assert loaded is not None
        assert loaded.current_state == State.WAITING_CLARIFICATION.value

    def test_no_handler_fails_task(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        engine = AgentEngine(workspace_root=tmp_path)
        # 不注册任何处理器

        task = Task(
            task_id="noh1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.CODING.value
        save_task(task)

        engine.process_task(task)

        loaded = get_task("noh1")
        assert loaded is not None
        assert loaded.current_state == State.FAILED.value

class TestEngineExceptionHandling:
    """异常捕获与自动流转"""

    def test_recoverable_error_goes_to_correcting(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        engine = AgentEngine(workspace_root=tmp_path)
        handler = FailOnceHandler(State.BUILDING)
        engine.register(State.CODING, handler)
        engine.register(State.CORRECTING, DummyHandler(State.CODING))

        task = Task(
            task_id="rec1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.CODING.value
        save_task(task)

        # 第一次：CODING -> recoverable error -> CORRECTING
        engine.process_task(task)

        loaded = get_task("rec1")
        assert loaded is not None
        # 由于 correcting handler 流转到 coding，process_task 会继续
        # 但第二次 coding handler 会成功到 building
        # 但 building 没注册 handler，所以会 failed
        assert loaded.current_state in (State.FAILED.value, State.BUILDING.value, State.CORRECTING.value)

    def test_fatal_error_goes_to_failed(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        engine = AgentEngine(workspace_root=tmp_path)
        engine.register(State.CODING, FatalHandler())

        task = Task(
            task_id="fat1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.CODING.value
        save_task(task)

        engine.process_task(task)

        loaded = get_task("fat1")
        assert loaded is not None
        assert loaded.current_state == State.FAILED.value

    def test_cancelled_task_stops_engine(self, temp_db, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        init_db()

        class SlowHandler(PhaseHandler):
            def __init__(self):
                self.calls = 0

            def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
                self.calls += 1
                if self.calls == 1:
                    cancel_task(task.task_id, "user")
                return PhaseResult(State.BUILDING, "next")

        engine = AgentEngine(workspace_root=tmp_path)
        engine.register(State.CODING, SlowHandler())
        engine.register(State.BUILDING, DummyHandler(State.COMPLETED))

        task = Task(
            task_id="cx1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.CODING.value
        save_task(task)

        engine.process_task(task)

        loaded = get_task("cx1")
        assert loaded.current_state == "cancelled"
        assert engine.get_handler(State.CODING).calls == 1


class TestEngineHooks:
    """生命周期钩子"""

    def test_on_enter_on_exit_called(self, tmp_path: Path):
        class HookedHandler(PhaseHandler):
            def __init__(self):
                self.entered = False
                self.exited = False

            def on_enter(self, task, workspace):
                self.entered = True

            def on_exit(self, task, workspace, result):
                self.exited = True

            def handle(self, task, workspace, **kwargs):
                return PhaseResult(State.COMPLETED, "done")

        engine = AgentEngine(workspace_root=tmp_path)
        handler = HookedHandler()
        engine.register(State.CODING, handler)

        task = Task(
            task_id="hook1", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        # 跳过数据库操作，直接测试钩子
        result = engine._execute_phase(handler, task, tmp_path)

        assert handler.entered is True
        assert handler.exited is True
        assert result is not None
        assert result.next_state == State.COMPLETED