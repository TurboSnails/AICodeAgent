#!/usr/bin/env python3
"""paths 模块单元测试"""

from utils import paths


def test_project_root_points_to_android_repo():
    assert paths.AGENT_ROOT.name == "AICodeAgent"
    assert (paths.PROJECT_ROOT / "app").is_dir()
    assert paths.WORKSPACE_ROOT == paths.AGENT_ROOT / "workspace"
