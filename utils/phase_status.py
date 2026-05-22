#!/usr/bin/env python3
"""任务工作区阶段状态 — 供 Web UI 展示当前在做什么。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logging_config import get_logger

logger = get_logger(__name__)

STATUS_FILE = "phase_status.json"


def write_phase_status(
    workspace: Path,
    state: str,
    detail: str,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "detail": detail,
        "updated_at": datetime.now().isoformat(),
        "extra": extra or {},
    }
    path = workspace / STATUS_FILE
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("write_phase_status failed: %s", e)


def read_phase_status(workspace: Path) -> dict[str, Any]:
    path = workspace / STATUS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("read_phase_status failed: %s", e)
        return {}
