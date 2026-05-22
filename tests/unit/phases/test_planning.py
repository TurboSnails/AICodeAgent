"""
PlanningHandler 单元测试
覆盖：auto level 判定、L0 快速路径、需求澄清判断、文档生成
"""
import sys
from pathlib import Path

import pytest

from engine.state_machine import State, Task
from phases.planning import PlanningHandler

class TestAutoLevel:
    """任务等级自动判定"""

    def test_l0_keywords(self):
        handler = PlanningHandler()
        assert handler._auto_level("修改 strings.xml 的 app_name 文案") == "L0"
        assert handler._auto_level("修复颜色值 #FF0000") == "L0"
        assert handler._auto_level("改个字") == "L0"

    def test_l2_keywords(self):
        handler = PlanningHandler()
        assert handler._auto_level("重构网络层架构") == "L2"
        assert handler._auto_level("数据库迁移方案") == "L2"
        assert handler._auto_level("全站主题系统设计") == "L2"

    def test_l1_default(self):
        handler = PlanningHandler()
        assert handler._auto_level("新增用户资料页面") == "L1"
        assert handler._auto_level("添加订单列表功能") == "L1"

class TestClarityAssessment:
    """需求澄清判断"""

    def test_clear_requirement(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases.planning.cfg_bool", lambda k, default=False: False)
        handler = PlanningHandler()
        task = Task(
            task_id="t1", raw_requirement="在 UserProfileScreen 添加头像上传功能，站点 haobo，需验证图片格式",
            level="L1", site_hint="haobo", source="test", chat_id="",
        )
        needs, questions, reason = handler._assess_clarity(task, tmp_path)
        assert needs is False or len(questions) == 0

    def test_unclear_requirement(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases.planning.cfg_bool", lambda k, default=False: False)
        handler = PlanningHandler()
        task = Task(
            task_id="t2", raw_requirement="改一下那个页面",
            level="L1", site_hint="", source="test", chat_id="",
        )
        needs, questions, reason = handler._assess_clarity(task, tmp_path)
        assert needs is True
        assert len(questions) > 0

    def test_skip_by_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("phases.planning.cfg_bool", lambda k, default=False: True)
        handler = PlanningHandler()
        task = Task(
            task_id="t3", raw_requirement="模糊需求",
            level="L1", site_hint="", source="test", chat_id="",
        )
        needs, questions, reason = handler._assess_clarity(task, tmp_path)
        assert needs is False
        assert reason == "skip by config"

class TestDocumentGeneration:
    """文档生成"""

    def test_generate_docs(self, tmp_path: Path):
        handler = PlanningHandler()
        task = Task(
            task_id="t4", raw_requirement="测试需求", level="L1",
            site_hint="haobo", source="test", chat_id="",
        )
        handler._generate_docs(task, tmp_path)

        req_file = tmp_path / "requirement.md"
        assert req_file.exists()
        content = req_file.read_text(encoding="utf-8")
        assert "测试需求" in content
        assert "L1" in content

    def test_prepare_asset_context(self, tmp_path: Path):
        handler = PlanningHandler()
        task = Task(
            task_id="t5", raw_requirement="Figma 设计稿页面", level="L1",
            site_hint="haobo", source="test", chat_id="",
        )
        handler._prepare_asset_context(task, tmp_path)

        analysis_file = tmp_path / "asset_analysis.json"
        assert analysis_file.exists()
        import json
        data = json.loads(analysis_file.read_text(encoding="utf-8"))
        assert data["task_id"] == "t5"
        assert data["has_figma_hint"] is True

class TestL0FastPath:
    """L0 快速路径"""

    def test_l0_skips_debate(self, tmp_path: Path):
        handler = PlanningHandler()
        task = Task(
            task_id="t6", raw_requirement="修改文案", level="L0",
            site_hint="", source="test", chat_id="",
        )
        handler._bootstrap_l0_consensus(task, tmp_path)

        consensus = tmp_path / "consensus.md"
        assert consensus.exists()
        content = consensus.read_text(encoding="utf-8")
        assert "L0" in content
        assert "快速路径" in content