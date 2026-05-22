"""
state_machine.py 单元测试
覆盖：状态定义、合法/非法流转、任务 CRUD、超时清理
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 将 orchestrator 加入 sys.path
import sys

from engine.state_machine import (
    State, VALID_TRANSITIONS, init_db, save_task, get_task, transition,
    get_executable_tasks, approve_gate_resume, approve_clarification_reply,
    cancel_task, get_all_non_terminal_tasks, get_task_state_history,
    get_waiting_gates, get_waiting_clarifications, Task, DB_FILE,
)

class TestStateDefinitions:
    """状态定义与合法流转矩阵"""

    def test_all_non_terminal_states_have_valid_transitions(self):
        """非终态必须在 VALID_TRANSITIONS 中有定义"""
        terminal = {State.COMPLETED, State.FAILED, State.CANCELLED}
        for s in State:
            if s not in terminal:
                assert s in VALID_TRANSITIONS, f"{s.value} 缺少合法流转定义"

    def test_terminal_states_no_outbound(self):
        """终态不能有出边（除非测试遗漏）"""
        terminal = {State.COMPLETED, State.FAILED, State.CANCELLED}
        for s in terminal:
            # 允许 cancel 从任意非终态跳转到 CANCELLED，但终态自身不应有出边
            assert VALID_TRANSITIONS.get(s, []) == [], f"{s.value} 不应有后续流转"

class TestDatabaseLifecycle:
    """数据库初始化与迁移"""

    def test_init_db_creates_tables(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr(
            "engine.state_machine.DB_FILE", db,
            raising=False,
        )
        init_db()
        conn = sqlite3.connect(str(db))
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_queue" in tables
        assert "state_history" in tables
        conn.close()

class TestTaskCRUD:
    """任务增删改查"""

    def test_save_and_get_task(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        task = Task(
            task_id="t1234567",
            raw_requirement="测试需求",
            level="L1",
            site_hint="haobo",
            source="test",
            chat_id="",
        )
        saved = save_task(task)
        assert saved.task_id == "t1234567"

        loaded = get_task("t1234567")
        assert loaded is not None
        assert loaded.raw_requirement == "测试需求"
        assert loaded.current_state == "pending"

    def test_get_nonexistent_task(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        assert get_task("noexist") is None

class TestStateTransition:
    """状态流转校验"""

    def test_valid_transition(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="tx1", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        ok = transition("tx1", State.PLANNING, "start")
        assert ok is True
        t = get_task("tx1")
        assert t.current_state == "planning"

    def test_invalid_transition_rejected(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="tx2", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        # pending -> coding 是非法的
        ok = transition("tx2", State.CODING, "illegal jump")
        assert ok is False
        t = get_task("tx2")
        assert t.current_state == "pending"

    def test_transition_history(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="tx3", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        transition("tx3", State.PLANNING, "start")
        transition("tx3", State.DEBATING, "debate")
        history = get_task_state_history("tx3")
        assert len(history) == 2
        assert history[0]["from_state"] == "pending"
        assert history[0]["to_state"] == "planning"

class TestGateAndClarification:
    """L2 核准与需求澄清续跑"""

    def test_approve_gate_resume(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="g1", raw_requirement="r", level="L2", site_hint="", source="test", chat_id=""))
        transition("g1", State.PLANNING, "start")
        transition("g1", State.DEBATING, "debate")
        transition("g1", State.CONSENSUS, "consensus")
        transition("g1", State.WAITING_GATE, "gate")

        ok = approve_gate_resume("g1")
        assert ok is True
        t = get_task("g1")
        assert t.current_state == "pending"
        assert t.resume_from_gate == 1

    def test_approve_clarification_reply(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="c1", raw_requirement="r", level="L1", site_hint="", source="test", chat_id=""))
        transition("c1", State.PLANNING, "start")
        transition("c1", State.WAITING_CLARIFICATION, "clarify")

        ok = approve_clarification_reply("c1", "补充说明")
        assert ok is True
        t = get_task("c1")
        assert t.current_state == "pending"
        assert "补充说明" in t.raw_requirement
        assert t.resume_after_clarification == 1

class TestCancel:
    """取消任务"""

    def test_cancel_non_terminal(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="k1", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        assert cancel_task("k1", "user") is True
        t = get_task("k1")
        assert t.current_state == "cancelled"

    def test_cancel_terminal_is_noop(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="k2", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        # 直接通过 SQL 将状态设为 completed（绕过状态机，因为 pending->completed 无合法流转）
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE task_queue SET current_state = ? WHERE task_id = ?", ("completed", "k2"))
        conn.commit()
        conn.close()
        assert cancel_task("k2", "user") is False

class TestQueryHelpers:
    """查询辅助函数"""

    def test_get_executable_tasks(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="q1", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        save_task(Task(task_id="q2", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        tasks = get_executable_tasks(limit=10)
        assert len(tasks) == 2

    def test_get_waiting_gates(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="wg1", raw_requirement="r", level="L2", site_hint="", source="test", chat_id=""))
        transition("wg1", State.PLANNING, "start")
        transition("wg1", State.DEBATING, "debate")
        transition("wg1", State.CONSENSUS, "consensus")
        transition("wg1", State.WAITING_GATE, "gate")
        gates = get_waiting_gates()
        assert len(gates) == 1
        assert gates[0].task_id == "wg1"

    def test_get_non_terminal_tasks(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "agent.db"
        monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
        init_db()
        save_task(Task(task_id="nt1", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        save_task(Task(task_id="nt2", raw_requirement="r", level="L0", site_hint="", source="test", chat_id=""))
        # 直接 SQL 设置终态
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE task_queue SET current_state = ? WHERE task_id = ?", ("completed", "nt2"))
        conn.commit()
        conn.close()
        non_term = get_all_non_terminal_tasks()
        assert len(non_term) == 1
        assert non_term[0].task_id == "nt1"