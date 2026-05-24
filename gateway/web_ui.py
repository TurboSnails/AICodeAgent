#!/usr/bin/env python3
"""
Web UI 网关 — V4 重构适配器
向后兼容 gateway/web_ui_v2.py 的 HTTP 接口，内部委托给 TaskService。

提供路由：
  GET  /                         — 重定向到 /static/chat.html
  GET  /health                   — 健康检查
  GET  /tasks                    — 任务列表
  GET  /task/:id                 — 任务详情
  GET  /task/:id/history         — 状态流转历史
  GET  /static/*                 — 静态文件服务
  GET  /uploads/:filename        — 上传图片服务
  GET  /api/stream/:id           — SSE 实时任务流
  POST /api/task                 — 提交任务（支持 image_urls）
  POST /api/continue/:id         — L2 核准
  POST /api/reply/:id            — 需求澄清回复（支持 image_urls）
  POST /api/cancel/:id           — 取消任务
  POST /api/upload               — 上传图片（base64 JSON）
  POST /api/cleanup/:id          — 强制清理垃圾任务
"""

from __future__ import annotations

import base64
import fcntl
import http.server
import json
import mimetypes
import os
import re
import socketserver
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine.state_machine import (
    DB_FILE,
    State,
    get_task_state_history,
    _connect,
    _row_to_task,
)
from utils.config_loader import cfg_str
from utils.logging_config import get_logger
from utils.request_logger import log_request
from services.task_service import TaskService
from utils.paths import DATA_DIR, WORKSPACE_ROOT
from utils.phase_status import read_phase_status

logger = get_logger(__name__)
from utils.paths import PROJECT_ROOT

# 静态文件根目录
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 上传图片目录
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Executor 运行时文件（用于判断是否有执行器在跑）
LOCK_FILE = DATA_DIR / "executor.lock"
CURRENT_TASK_FILE = DATA_DIR / "executor.current_task"

# 判定为垃圾任务的空闲阈值（秒）
ORPHAN_IDLE_THRESHOLD = 600  # 10 分钟

# 终态与等待态
_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_WAITING_STATES = {"waiting_gate", "waiting_clarification"}

# SSE 流最大持续时间（秒）
_SSE_MAX_SECONDS = 600


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
    # SSE 实时任务流
    # ------------------------------------------------------------------

    def _handle_sse(self, task_id: str) -> None:
        """Server-Sent Events：实时推送任务状态变更和编码日志。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def _emit(data: dict) -> bool:
            try:
                line = "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        last_state: str | None = None
        last_activity_hash: int | None = None
        log_offset: int = 0
        deadline = time.monotonic() + _SSE_MAX_SECONDS

        try:
            while time.monotonic() < deadline:
                task = self._task_service.get_task(task_id)
                if not task:
                    _emit({"type": "error", "error": "task not found"})
                    break

                # 编码日志增量推送
                if task.current_state == "coding":
                    log_path = WORKSPACE_ROOT / task_id / "coding_cli.log"
                    if log_path.is_file():
                        try:
                            content = log_path.read_text(encoding="utf-8", errors="replace")
                            new_text = content[log_offset:]
                            if new_text:
                                log_offset = len(content)
                                if not _emit({"type": "log", "text": new_text}):
                                    break
                        except OSError:
                            pass

                # 状态或 activity 有变化时推送完整快照
                state = task.current_state
                activity = _load_task_activity(task)
                activity_hash = hash(json.dumps(activity, sort_keys=True, ensure_ascii=False))

                if state != last_state or activity_hash != last_activity_hash:
                    last_state = state
                    last_activity_hash = activity_hash
                    payload: dict = {**_task_to_dict(task), **activity, "type": "state"}
                    # 附带图片引用
                    payload["image_urls"] = _load_image_refs(task_id)
                    if not _emit(payload):
                        break

                # 终态：再推一次 done 并退出
                if state in _TERMINAL_STATES:
                    _emit({"type": "done"})
                    break

                time.sleep(1)
        except Exception:
            pass  # 连接断开或其他异常，静默退出

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
        with log_request(self, logger) as ctx:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                self.send_response(301)
                self.send_header("Location", "/static/chat.html")
                self.end_headers()
                ctx.status = 301
                return

            if path == "/health":
                self._json({"status": "ok", "version": "v4"})
                ctx.status = 200
                return

            if path == "/api/orphan-tasks":
                result = _detect_orphan_tasks()
                self._json(result)
                ctx.status = 200
                return

            if path == "/tasks":
                tasks = self._task_service.list_tasks(limit=50)
                self._json({"tasks": [_task_to_dict(t) for t in tasks]})
                ctx.status = 200
                return

            m = re.match(r"/task/([a-zA-Z0-9_-]+)/history", path)
            if m:
                task_id = m.group(1)
                history = get_task_state_history(task_id)
                self._json({"task_id": task_id, "history": history})
                ctx.status = 200
                return

            m = re.match(r"/task/([a-zA-Z0-9_-]+)", path)
            if m:
                task_id = m.group(1)
                task = self._task_service.get_task(task_id)
                if task:
                    self._json({"task": _task_to_dict(task)})
                    ctx.status = 200
                else:
                    self._json({"error": "task not found"}, 404)
                    ctx.status = 404
                return

            # SSE 实时任务流
            m = re.match(r"/api/stream/([a-zA-Z0-9_-]+)$", path)
            if m:
                task_id = m.group(1)
                self._handle_sse(task_id)
                ctx.status = 200
                return

            # 上传图片服务
            if path.startswith("/uploads/"):
                filename = path[len("/uploads/"):]
                if re.match(r"^[a-zA-Z0-9_.-]+$", filename):
                    img_path = UPLOADS_DIR / filename
                    if img_path.is_file():
                        ct, _ = mimetypes.guess_type(str(img_path))
                        self.send_response(200)
                        self.send_header("Content-Type", ct or "application/octet-stream")
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        self.wfile.write(img_path.read_bytes())
                        ctx.status = 200
                        return
                self._json({"error": "not found"}, 404)
                ctx.status = 404
                return

            # 静态文件服务
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
                ctx.status = 200
                return

            self._json({"error": "not found"}, 404)
            ctx.status = 404

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        with log_request(self, logger) as ctx:
            if not _auth_ok(dict(self.headers)):
                self._json({"error": "unauthorized"}, 401)
                ctx.status = 401
                return

            parsed = urlparse(self.path)
            path = parsed.path

            # 图片上传（base64 JSON）
            if path == "/api/upload":
                payload = self._read_body()
                images = payload.get("images") or []
                saved: list[dict] = []
                for img in images:
                    try:
                        name = (img.get("name") or "image.png").replace("/", "_")
                        data_b64 = img.get("data") or ""
                        mime = img.get("mime_type") or "image/png"
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
                        if ext not in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
                            ext = "png"
                        file_id = uuid.uuid4().hex[:12]
                        dest = UPLOADS_DIR / f"{file_id}.{ext}"
                        dest.write_bytes(base64.b64decode(data_b64))
                        saved.append({"file_id": file_id, "url": f"/uploads/{file_id}.{ext}", "name": name})
                    except Exception as exc:
                        logger.warning("upload error: %s", exc)
                self._json({"files": saved})
                ctx.status = 200
                return

            # 提交任务
            if path == "/api/task":
                payload = self._read_body()
                requirement = (payload.get("requirement") or "").strip()
                if not requirement:
                    self._json({"error": "requirement is required"}, 400)
                    ctx.status = 400
                    return
                level = payload.get("level", "auto")
                site_hint = payload.get("site_hint", "")
                source = payload.get("source", "web")
                chat_id = payload.get("chat_id", "")
                request_type = payload.get("request_type", "")
                image_urls: list[str] = payload.get("image_urls") or []
                task = self._task_service.submit_task(
                    requirement=requirement,
                    level=level,
                    site_hint=site_hint,
                    source=source,
                    chat_id=chat_id,
                    request_type=request_type,
                )
                # 保存图片引用到工作区（规划阶段可见）
                if image_urls:
                    _save_image_refs(task.task_id, image_urls)
                self._json({"task_id": task.task_id, "level": task.level, "status": task.status, "request_type": task.request_type})
                ctx.status = 200
                return

            # L2 核准
            m = re.match(r"/api/continue/([a-zA-Z0-9_-]+)", path)
            if m:
                task_id = m.group(1)
                ok = self._task_service.approve_gate(task_id)
                self._json({"success": ok, "task_id": task_id})
                ctx.status = 200 if ok else 400
                return

            # 需求澄清回复
            m = re.match(r"/api/reply/([a-zA-Z0-9_-]+)", path)
            if m:
                task_id = m.group(1)
                payload = self._read_body()
                reply = (payload.get("reply") or "").strip()
                if not reply:
                    self._json({"error": "reply is required"}, 400)
                    ctx.status = 400
                    return
                image_urls_reply: list[str] = payload.get("image_urls") or []
                if image_urls_reply:
                    _append_image_refs(task_id, image_urls_reply)
                ok = self._task_service.submit_clarification(task_id, reply)
                self._json({"success": ok, "task_id": task_id})
                ctx.status = 200 if ok else 400
                return

            # 取消任务
            m = re.match(r"/api/cancel/([a-zA-Z0-9_-]+)", path)
            if m:
                task_id = m.group(1)
                payload = self._read_body()
                reason = payload.get("reason", "user cancelled")
                ok = self._task_service.cancel(task_id, reason)
                self._json({"success": ok, "task_id": task_id})
                ctx.status = 200 if ok else 400
                return

            # 清理（强制失败）垃圾任务
            m = re.match(r"/api/cleanup/([a-zA-Z0-9_-]+)", path)
            if m:
                task_id = m.group(1)
                ok = _cleanup_orphan_task(task_id)
                self._json({"success": ok, "task_id": task_id})
                ctx.status = 200 if ok else 400
                return

            self._json({"error": "not found"}, 404)
            ctx.status = 404


# ------------------------------------------------------------------
# 图片引用辅助
# ------------------------------------------------------------------


def _save_image_refs(task_id: str, image_urls: list[str]) -> None:
    """创建任务时，将图片 URL 列表写入工作区 image_refs.json。"""
    ws = WORKSPACE_ROOT / task_id
    ws.mkdir(parents=True, exist_ok=True)
    refs_path = ws / "image_refs.json"
    try:
        existing = json.loads(refs_path.read_text()) if refs_path.exists() else []
        merged = existing + [u for u in image_urls if u not in existing]
        refs_path.write_text(json.dumps(merged, ensure_ascii=False))
    except Exception:
        pass


def _append_image_refs(task_id: str, image_urls: list[str]) -> None:
    """澄清回复时，追加图片引用到 image_refs.json。"""
    _save_image_refs(task_id, image_urls)


def _load_image_refs(task_id: str) -> list[str]:
    """读取任务工作区的图片引用列表。"""
    refs_path = WORKSPACE_ROOT / task_id / "image_refs.json"
    try:
        if refs_path.exists():
            return json.loads(refs_path.read_text())
    except Exception:
        pass
    return []


# ------------------------------------------------------------------
# 孤儿任务检测与清理
# ------------------------------------------------------------------


def _is_executor_running() -> tuple[bool, str]:
    """检测 executor 是否在运行，返回 (是否运行, 当前处理的任务ID)"""
    if not LOCK_FILE.exists():
        return False, ""
    try:
        fd = os.open(str(LOCK_FILE), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 能拿到锁说明没有 executor 在跑
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            return False, ""
        except BlockingIOError:
            os.close(fd)
            # 拿不到锁，说明 executor 在跑
            current_task = ""
            if CURRENT_TASK_FILE.exists():
                current_task = CURRENT_TASK_FILE.read_text(encoding="utf-8").strip()
            return True, current_task
    except OSError:
        return False, ""


def _parse_iso_datetime(iso_str: str) -> datetime | None:
    """解析 ISO 格式时间字符串"""
    if not iso_str:
        return None
    try:
        # 处理带 Z 和不带 Z 的情况
        s = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _detect_orphan_tasks() -> dict:
    """检测垃圾任务：非终态、非等待态、长时间未更新的任务"""
    executor_running, current_task_id = _is_executor_running()

    conn = _connect()
    cursor = conn.execute(
        "SELECT * FROM task_queue WHERE current_state NOT IN (?, ?, ?)",
        tuple(_TERMINAL_STATES),
    )
    rows = cursor.fetchall()
    conn.close()

    orphans = []
    now = datetime.now(timezone.utc)

    for row in rows:
        state = row["current_state"]
        task_id = row["task_id"]

        # 等待态不算垃圾
        if state in _WAITING_STATES:
            continue

        # pending 不算垃圾（正常排队）
        if state == "pending":
            continue

        # executor 正在跑且当前处理的就是这个任务，不算垃圾
        if executor_running and task_id == current_task_id:
            continue

        # 检查更新时间
        updated = _parse_iso_datetime(row["updated_at"] or "")
        if updated is None:
            updated = _parse_iso_datetime(row["created_at"] or "") or now

        # 统一为 UTC 进行比较
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)

        idle_seconds = int((now - updated).total_seconds())

        # executor 没在跑：所有非终态非等待态都是垃圾
        # executor 在跑：超过阈值才算垃圾
        is_orphan = not executor_running or idle_seconds >= ORPHAN_IDLE_THRESHOLD

        if is_orphan:
            orphans.append({
                "task_id": task_id,
                "current_state": state,
                "raw_requirement": (row["raw_requirement"] or "")[:80],
                "level": row["level"] or "",
                "updated_at": row["updated_at"] or "",
                "created_at": row["created_at"] or "",
                "idle_seconds": idle_seconds,
                "idle_minutes": idle_seconds // 60,
            })

    return {
        "executor_running": executor_running,
        "current_task_id": current_task_id,
        "orphan_count": len(orphans),
        "orphans": orphans,
    }


def _cleanup_orphan_task(task_id: str) -> bool:
    """将垃圾任务强制流转到 failed"""
    from engine.state_machine import cancel_task, get_task

    task = get_task(task_id)
    if not task:
        return False
    if task.current_state in _TERMINAL_STATES:
        return False
    # 使用 cancel 而不是直接改状态，保持审计历史
    return cancel_task(task_id, reason="orphan cleanup via web ui")


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------


_ARTIFACT_BY_REQUEST_TYPE: dict[str, list[tuple[str, str]]] = {
    "explain": [("answer", "answer.md")],
    "design_only": [("design", "design.md")],
    "review_only": [("review", "codex_review.md"), ("review", "requirement_review.md")],
}
_ARTIFACT_FALLBACK: list[tuple[str, str]] = [
    ("answer", "answer.md"),
    ("design", "design.md"),
    ("review", "codex_review.md"),
    ("consensus", "consensus.md"),
]
_ARTIFACT_TITLES = {
    "answer": "AI 回答",
    "design": "设计方案",
    "review": "审查报告",
    "consensus": "共识文档",
}
_ARTIFACT_MAX_FULL = 200_000
_ARTIFACT_MAX_PREVIEW = 600


_CODE_PROGRESS: dict[str, int] = {
    "pending": 0,
    "planning": 5,
    "waiting_clarification": 5,
    "debating": 15,
    "consensus": 25,
    "architect_planning": 35,
    "waiting_gate": 40,
    "direct_answer": 0,  # explain 流水线会覆盖
    "design_output": 0,
    "coding": 50,
    "building": 60,
    "self_review": 70,
    "codex_review": 75,
    "architect_review": 80,
    "red_team_review": 85,
    "requirement_review": 90,
    "correcting": 55,
    "git_committing": 95,
    "creating_pr": 97,
    "notifying": 98,
    "completed": 100,
    "failed": 100,
    "cancelled": 100,
}

_PIPELINE_PROGRESS: dict[str, dict[str, int]] = {
    "explain": {
        "pending": 0,
        "planning": 20,
        "waiting_clarification": 10,
        "direct_answer": 75,
        "completed": 100,
        "failed": 100,
        "cancelled": 100,
    },
    "design_only": {
        "pending": 0,
        "planning": 15,
        "architect_planning": 45,
        "design_output": 85,
        "waiting_clarification": 10,
        "completed": 100,
        "failed": 100,
        "cancelled": 100,
    },
    "review_only": {
        "pending": 0,
        "planning": 15,
        "consensus": 30,
        "codex_review": 60,
        "architect_review": 70,
        "red_team_review": 80,
        "requirement_review": 90,
        "completed": 100,
        "failed": 100,
        "cancelled": 100,
    },
}


def _failed_at_state(task_id: str, terminal: str) -> str:
    """终态失败/取消时，取进入终态前的阶段（供进度条锚点）。"""
    if terminal not in ("failed", "cancelled"):
        return ""
    for entry in reversed(get_task_state_history(task_id)):
        if entry.get("to_state") == terminal:
            return entry.get("from_state") or ""
    return ""


def _progress_for_task(task) -> int:
    """按 request_type 选用短流水线进度，避免 explain 在 direct_answer 显示 0%。"""
    rt = getattr(task, "request_type", "code") or "code"
    st = task.current_state
    if st in ("failed", "cancelled"):
        anchor = _failed_at_state(task.task_id, st) or "building"
        pipeline = _PIPELINE_PROGRESS.get(rt)
        if pipeline and anchor in pipeline:
            return pipeline[anchor]
        return _CODE_PROGRESS.get(anchor, _CODE_PROGRESS.get(st, 0))
    pipeline = _PIPELINE_PROGRESS.get(rt)
    if pipeline and st in pipeline:
        return pipeline[st]
    return _CODE_PROGRESS.get(st, 0)


def _load_task_artifact(task) -> dict:
    """读取工作区产物（explain → answer.md 等），供 Web 展示。"""
    if task.current_state != "completed":
        return {}

    ws = WORKSPACE_ROOT / task.task_id
    if not ws.is_dir():
        return {}

    rt = getattr(task, "request_type", "code") or "code"
    candidates = list(_ARTIFACT_BY_REQUEST_TYPE.get(rt, [])) + _ARTIFACT_FALLBACK
    seen: set[str] = set()

    for kind, filename in candidates:
        if filename in seen:
            continue
        seen.add(filename)
        path = ws / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.debug("read artifact %s failed: %s", path, e)
            continue
        if not text:
            continue

        preview = text[:_ARTIFACT_MAX_PREVIEW]
        if len(text) > _ARTIFACT_MAX_PREVIEW:
            preview += "…"

        return {
            "artifact_type": kind,
            "artifact_title": _ARTIFACT_TITLES.get(kind, "输出"),
            "artifact_content": text[:_ARTIFACT_MAX_FULL],
            "artifact_preview": preview,
            "artifact_truncated": len(text) > _ARTIFACT_MAX_FULL,
        }

    return {}


def _task_to_dict(task) -> dict:
    """将 Task 对象转为 JSON 字典，同时读取工作区的澄清问题（如果存在）"""
    # 尝试读取 clarification_questions.md（waiting_clarification 状态用）
    questions = []
    if task.current_state == "waiting_clarification":
        cq_path = WORKSPACE_ROOT / task.task_id / "clarification_questions.md"
        try:
            if cq_path.exists():
                text = cq_path.read_text(encoding="utf-8")
                # 简单解析 Markdown 列表：提取 "1. xxx" 或 "- xxx" 行
                for line in text.splitlines():
                    m = re.match(r"^\s*(?:\d+\.\s+|[-*]\s+)(.+)", line)
                    if m:
                        questions.append(m.group(1).strip())
        except Exception:
            pass

    progress = _progress_for_task(task)
    artifact = _load_task_artifact(task)
    phase = _load_task_activity(task)
    failed_at = _failed_at_state(task.task_id, task.current_state)

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
        "request_type": getattr(task, "request_type", "code"),
        "clarification_questions": questions,
        "progress": progress,
        "failed_at_state": failed_at,
        "image_urls": _load_image_refs(task.task_id),
        **phase,
        **artifact,
    }


_ACTIVITY_PREVIEW_MAX = 900


def _load_phase_detail(task) -> dict:
    """读取工作区 phase_status.json，供 Web 展示当前子步骤。"""
    ws = WORKSPACE_ROOT / task.task_id
    data = read_phase_status(ws)
    if not data:
        return {}
    detail = str(data.get("detail", "")).strip()
    updated = str(data.get("updated_at", "")).strip()
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    hint = str(extra.get("hint", "")).strip() if extra else ""
    phase_detail = detail
    if hint and hint not in detail:
        phase_detail = f"{detail} — {hint}" if detail else hint
    return {
        "phase_status_state": str(data.get("state", "")),
        "phase_detail": phase_detail,
        "phase_updated_at": updated,
        "phase_extra": extra,
    }


def _sanitize_cli_feedback(text: str, *, max_lines: int = 16) -> str:
    """Web 展示用 CLI 日志：去掉超长 cmd 行，保留最近若干行。"""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("cmd:") and len(line) > 240:
            line = line[:200].rstrip() + f" … (truncated, total {len(line)} chars)"
        lines.append(line)
    return "\n".join(lines[-max_lines:])


def _load_task_activity(task) -> dict:
    """阶段说明 + 诊断 / Claude 输出摘要（进行中与失败时尤其有用）。"""
    result = _load_phase_detail(task)
    ws = WORKSPACE_ROOT / task.task_id
    if not ws.is_dir():
        return result

    extra = result.get("phase_extra") or {}
    if isinstance(extra, dict):
        cli_tail = str(extra.get("cli_log_tail", "")).strip()
        cli_status = str(extra.get("cli_status", "")).strip()
        cli_running = extra.get("cli_running")
        cli_pid = extra.get("cli_pid")
        if cli_tail or cli_status or cli_pid:
            result["cli_feedback"] = _sanitize_cli_feedback(cli_tail)
            result["cli_status"] = cli_status
            result["cli_running"] = bool(cli_running)
            result["cli_pid"] = cli_pid
        log_path = ws / "coding_cli.log"
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-12:])
                if tail and not result.get("cli_feedback"):
                    result["cli_feedback"] = _sanitize_cli_feedback(tail)
                if result.get("cli_running") is None and task.current_state == "coding":
                    result["cli_running"] = True
                    result.setdefault("cli_status", "running")
            except OSError:
                pass

    snippets: list[str] = []
    diag_path = ws / "coding_apply_diag.json"
    if diag_path.is_file():
        try:
            diag = json.loads(diag_path.read_text(encoding="utf-8"))
            if isinstance(diag, dict):
                markers = diag.get("file_markers_found")
                applied = diag.get("applied_count")
                if markers is not None:
                    snippets.append(f"解析: 发现 {markers} 个 FILE 块，已写入 {applied} 个文件")
                if diag.get("hint"):
                    snippets.append(str(diag["hint"]))
        except (OSError, json.JSONDecodeError):
            pass

    out_path = ws / "last_claude_output.md"
    if out_path.is_file():
        try:
            text = out_path.read_text(encoding="utf-8").strip()
            if text:
                snippets.append(text[:_ACTIVITY_PREVIEW_MAX] + ("…" if len(text) > _ACTIVITY_PREVIEW_MAX else ""))
        except OSError:
            pass

    if snippets:
        result["ai_output_preview"] = "\n".join(snippets)
    return result


# 兼容旧版入口：直接运行此文件时启动 HTTP 服务器
if __name__ == "__main__":
    PORT = int(os.environ.get("AGENT_WEB_PORT", cfg_str("gateway.web_port", "6789")))

    class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        """多线程 TCP 服务器，支持 SSE 长连接与普通请求并发。"""
        allow_reuse_address = True
        daemon_threads = True  # 主进程退出时不等待 SSE 线程

    _task_svc = TaskService()
    with _ThreadingServer(("", PORT), lambda *a, **kw: WebUIHandler(*a, task_service=_task_svc, **kw)) as httpd:
        logger.info("Web UI (threaded) serving on port %d", PORT)
        httpd.serve_forever()
