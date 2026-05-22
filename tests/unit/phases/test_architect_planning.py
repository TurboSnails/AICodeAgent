"""
phases/architect_planning.py 单元测试
覆盖：L0 跳过、不确定性解析、状态流转
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.state_machine import State, Task
from phases.architect_planning import ArchitectPlanningHandler


class TestArchitectPlanningHandler:
    def test_l0_skips_when_disabled(self, tmp_path: Path):
        handler = ArchitectPlanningHandler(ai_client=MagicMock())
        task = Task(
            task_id="ap1", raw_requirement="fix typo", level="L0",
            site_hint="", source="test", chat_id="",
        )
        with patch("phases.architect_planning.cfg_bool", return_value=False):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CODING
        assert "L0 skip" in result.reason

    def test_l0_runs_when_enabled(self, tmp_path: Path):
        ai = MagicMock()
        ai.call.return_value = """
## Plan
- step 1

## Design
- design 1

## Uncertainty Check
```json
{"uncertainties": [], "blocking": false}
```
"""
        handler = ArchitectPlanningHandler(ai_client=ai)
        task = Task(
            task_id="ap2", raw_requirement="refactor", level="L0",
            site_hint="", source="test", chat_id="",
        )
        with patch("phases.architect_planning.cfg_bool", return_value=True):
            with patch("phases.architect_planning.save_task"):
                result = handler.handle(task, tmp_path)
        assert result.next_state == State.CODING
        assert (tmp_path / "plan.md").exists()
        assert (tmp_path / "design.md").exists()

    def test_blocking_uncertainties_go_to_clarification(self, tmp_path: Path):
        ai = MagicMock()
        ai.call.return_value = """
## Uncertainty Check
```json
{"uncertainties": [{"question": "Q1", "severity": "blocking", "affected_files": []}], "blocking": true}
```
"""
        handler = ArchitectPlanningHandler(ai_client=ai, notification_service=MagicMock())
        task = Task(
            task_id="ap3", raw_requirement="complex feature", level="L1",
            site_hint="", source="test", chat_id="",
        )
        with patch("phases.architect_planning.cfg_bool", return_value=True):
            with patch("phases.architect_planning.save_task"):
                result = handler.handle(task, tmp_path)
        assert result.next_state == State.WAITING_CLARIFICATION
        assert task.clarification_type == "plan"
        assert (tmp_path / "uncertainty_check.json").exists()

    def test_parse_uncertainty_check(self, tmp_path: Path):
        handler = ArchitectPlanningHandler(ai_client=MagicMock())
        (tmp_path / "uncertainty_check.json").write_text(
            json.dumps(
                {
                    "uncertainties": [
                        {"question": "Q", "severity": "medium", "affected_files": ["A.kt"], "suggested_resolution": "S"}
                    ],
                    "blocking": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = handler._parse_uncertainty_check(tmp_path)
        assert result["blocking"] is False
        assert len(result["uncertainties"]) == 1

    def test_max_retry_exceeded(self, tmp_path: Path):
        ai = MagicMock()
        ai.call.return_value = "bad"
        handler = ArchitectPlanningHandler(ai_client=ai)
        task = Task(
            task_id="ap4", raw_requirement="r", level="L1",
            site_hint="", source="test", chat_id="",
            phase_counters={"architect_planning": 3},
        )
        with patch("phases.architect_planning.cfg_bool", return_value=True):
            with patch("phases.architect_planning.save_task"):
                with pytest.raises(Exception, match="max retries"):
                    handler.handle(task, tmp_path)
