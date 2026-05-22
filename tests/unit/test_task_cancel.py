"""任务协作式取消"""

from pathlib import Path
from unittest.mock import patch

import pytest

from engine.exceptions import TaskCancelledError
from engine.state_machine import Task, cancel_task, get_task, init_db, save_task
from utils import task_cancel


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "agent.db"
    monkeypatch.setattr("engine.state_machine.DB_FILE", db, raising=False)
    init_db()
    return db


class TestTaskCancelHelpers:
    def test_is_task_cancelled(self, temp_db, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        save_task(Task(task_id="c1", raw_requirement="r", level="L0", site_hint="", source="t", chat_id=""))
        assert task_cancel.is_task_cancelled("c1") is False
        cancel_task("c1", "test")
        assert task_cancel.is_task_cancelled("c1") is True

    def test_raise_if_cancelled(self, temp_db, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        save_task(Task(task_id="c2", raw_requirement="r", level="L0", site_hint="", source="t", chat_id=""))
        cancel_task("c2", "test")
        task_cancel.set_active_task("c2")
        try:
            with pytest.raises(TaskCancelledError):
                task_cancel.raise_if_cancelled()
        finally:
            task_cancel.clear_active_task()

    @patch("utils.task_cancel.subprocess.run")
    def test_interrupt_when_current(self, mock_run, temp_db, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        ct = tmp_path / "executor.current_task"
        monkeypatch.setattr("utils.task_cancel.CURRENT_TASK_FILE", ct, raising=False)
        ct.write_text("run1", encoding="utf-8")
        task_cancel.set_active_task("run1")
        try:
            assert task_cancel.interrupt_running_work("run1") is True
            assert mock_run.call_count >= 1
            assert task_cancel.interrupt_running_work("other") is False
        finally:
            task_cancel.clear_active_task()

    @patch("utils.task_cancel.interrupt_running_work")
    def test_cancel_triggers_interrupt(self, mock_interrupt, temp_db, monkeypatch):
        monkeypatch.setattr("engine.state_machine.DB_FILE", temp_db, raising=False)
        save_task(Task(task_id="c3", raw_requirement="r", level="L0", site_hint="", source="t", chat_id=""))
        task_cancel.set_active_task("c3")
        try:
            cancel_task("c3", "user")
            mock_interrupt.assert_called_once_with("c3")
        finally:
            task_cancel.clear_active_task()
        assert get_task("c3").current_state == "cancelled"
