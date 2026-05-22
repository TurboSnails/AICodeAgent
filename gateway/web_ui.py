#!/usr/bin/env python3
"""
Web UI 网关 — V4 重构适配器
向后兼容 gateway/web_ui_v2.py 的 HTTP 接口，内部委托给 TaskService。

提供路由：
  GET  /                     — 重定向到 /static/index.html
  GET  /health               — 健康检查
  GET  /tasks                — 任务列表
  GET  /task/:id             — 任务详情
  GET  /task/:id/history     — 状态流转历史
  GET  /static/*             — 静态文件服务
  POST /api/task             — 提交任务
  POST /api/continue/:id     — L2 核准
  POST /api/reply/:id        — 需求澄清回复
  POST /api/cancel/:id       — 取消任务
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine.state_machine import get_task_state_history
from utils.config_loader import cfg_str
from utils.logging_config import get_logger
from services.task_service import TaskService

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT

# 静态文件根目录
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _auth_ok(headers: dict[str, str]) -> bool:
    webhook_secret = cfg_str("gateway.api_key", "")
    if not webhook_secret:
        return True
    auth = headers.get("Authorization", "")
    return auth == f"Bearer {webhook_secret}"


class WebUIHandler(http.server.BaseHTTPRequestHandler):
    """V4 Web UI HTTP 处理器"""

    def __init__(self, *args, task_service: TaskService | None = None, **kwargs):
        self._task_service = task_service or TaskService()
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:
        pass  # 静默日志，由应用层控制

    # ------------------------------------------------------------------
    # 响应辅助
    # ------------------------------------------------------------------

    def _json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, relative_path: str) -> None:
        """安全地提供静态文件"""
        # 防止路径遍历
        safe_path = (STATIC_DIR / relative_path).resolve()
        if not str(safe_path).startswith(str(STATIC_DIR.resolve())):
            self._json({"error": "forbidden"}, 403)
            return
        if not safe_path.exists() or safe_path.is_dir():
            self._json({"error": "not found"}, 404)
            return
        content_type, _ = mimetypes.guess_type(str(safe_path))
        if not content_type:
            content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(safe_path.read_bytes())

    # ------------------------------------------------------------------
    # CORS 预检
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_response(301)
            self.send_header("Location", "/static/index.html")
            self.end_headers()
            return

        if path == "/health":
            self._json({"status": "ok", "version": "v4"})
            return

        if path == "/tasks":
            tasks = self._task_service.list_tasks(limit=50)
            self._json({"tasks": [_task_to_dict(t) for t in tasks]})
            return

        m = re.match(r"/task/([a-zA-Z0-9_-]+)/history", path)
        if m:
            task_id = m.group(1)
            history = get_task_state_history(task_id)
            self._json({"task_id": task_id, "history": history})
            return

        m = re.match(r"/task/([a-zA-Z0-9_-]+)", path)
        if m:
            task_id = m.group(1)
            task = self._task_service.get_task(task_id)
            if task:
                self._json({"task": _task_to_dict(task)})
            else:
                self._json({"error": "task not found"}, 404)
            return

        # 静态文件服务
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        if not _auth_ok(dict(self.headers)):
            self._json({"error": "unauthorized"}, 401)
            return

        parsed = urlparse(self.path)
        path = parsed.path

        # 提交任务
        if path == "/api/task":
            payload = self._read_body()
            requirement = (payload.get("requirement") or "").strip()
            if not requirement:
                self._json({"error": "requirement is required"}, 400)
                return
            level = payload.get("level", "auto")
            site_hint = payload.get("site_hint", "")
            source = payload.get("source", "web")
            chat_id = payload.get("chat_id", "")
            task = self._task_service.submit_task(
                requirement=requirement,
                level=level,
                site_hint=site_hint,
                source=source,
                chat_id=chat_id,
            )
            self._json({"task_id": task.task_id, "level": task.level, "status": task.status})
            return

        # L2 核准
        m = re.match(r"/api/continue/([a-zA-Z0-9_-]+)", path)
        if m:
            task_id = m.group(1)
            ok = self._task_service.approve_gate(task_id)
            self._json({"success": ok, "task_id": task_id})
            return

        # 需求澄清回复
        m = re.match(r"/api/reply/([a-zA-Z0-9_-]+)", path)
        if m:
            task_id = m.group(1)
            payload = self._read_body()
            reply = (payload.get("reply") or "").strip()
            if not reply:
                self._json({"error": "reply is required"}, 400)
                return
            ok = self._task_service.submit_clarification(task_id, reply)
            self._json({"success": ok, "task_id": task_id})
            return

        # 取消任务
        m = re.match(r"/api/cancel/([a-zA-Z0-9_-]+)", path)
        if m:
            task_id = m.group(1)
            payload = self._read_body()
            reason = payload.get("reason", "user cancelled")
            ok = self._task_service.cancel(task_id, reason)
            self._json({"success": ok, "task_id": task_id})
            return

        self._json({"error": "not found"}, 404)


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------


def _task_to_dict(task) -> dict:
    return {
        "task_id": task.task_id,
        "raw_requirement": task.raw_requirement,
        "level": task.level,
        "site_hint": task.site_hint,
        "source": task.source,
        "status": task.status,
        "current_state": task.current_state,
        "attempt_count": task.attempt_count,
        "max_retries": task.max_retries,
        "pr_url": task.pr_url,
        "branch": task.branch,
        "base_branch": task.base_branch,
        "error_log": task.error_log,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "gate_deadline": task.gate_deadline,
        "clarification_deadline": task.clarification_deadline,
    }


# 兼容旧版入口：直接运行此文件时启动 HTTP 服务器
if __name__ == "__main__":
    import socketserver

    PORT = int(os.environ.get("AGENT_WEB_PORT", cfg_str("gateway.web_port", "6789")))
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    with ReusableTCPServer(("", PORT), lambda *a, **kw: WebUIHandler(*a, task_service=TaskService(), **kw)) as httpd:
        logger.info("Web UI serving on port %d", PORT)
        httpd.serve_forever()
