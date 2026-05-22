#!/usr/bin/env python3
"""
TencentDB Agent Memory — Gateway HTTP 客户端

对接 memory-tencentdb standalone Gateway（默认 http://127.0.0.1:8420）：
  POST /recall、POST /capture、POST /session/end、POST /search/memories

安装 Gateway（需 Node >= 22）：
  npm install @tencentdb-agent-memory/memory-tencentdb
  memory-tencentdb-ctl config llm --api-key ... --base-url ... --model ...
  memory-tencentdb-ctl start
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from utils.config_loader import cfg_bool, cfg_int, cfg_str
from utils.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT_GATEWAY = "http://127.0.0.1:8420"
_MAX_CAPTURE_CHARS = 12000


class TencentMemoryService:
    """TencentDB Agent Memory Gateway 封装；Gateway 不可用时静默降级。"""

    def __init__(self) -> None:
        self._gateway_base = cfg_str("memory.tencentdb.gateway_url", _DEFAULT_GATEWAY).rstrip("/")
        self._timeout = cfg_float_safe("memory.tencentdb.timeout_sec", 5.0)
        self._session_key = cfg_str("memory.tencentdb.session_key", "aicodeagent/wm")
        self._recall_enabled = cfg_bool("memory.tencentdb.recall", True)
        self._capture_enabled = cfg_bool("memory.tencentdb.capture", True)
        self._max_capture = cfg_int("memory.tencentdb.max_capture_chars", _MAX_CAPTURE_CHARS)

    @property
    def enabled(self) -> bool:
        return cfg_bool("memory.tencentdb.enabled", False)

    @property
    def gateway_url(self) -> str:
        return self._gateway_base

    def health_ok(self) -> bool:
        if not self.enabled:
            return False
        try:
            data = self._get("/health")
            return isinstance(data, dict) and data.get("status") in ("ok", "degraded")
        except Exception as e:
            logger.debug("TencentDB memory gateway health check failed: %s", e)
            return False

    def recall(self, query: str, session_key: Optional[str] = None) -> str:
        """召回长期记忆，注入 system context 文本。"""
        if not self.enabled or not self._recall_enabled:
            return ""
        key = session_key or self._session_key
        try:
            data = self._post(
                "/recall",
                {"query": query, "session_key": key},
            )
            ctx = (data or {}).get("context") or ""
            if ctx.strip():
                logger.info(
                    "Memory recall: %d chars, strategy=%s, count=%s",
                    len(ctx),
                    data.get("strategy"),
                    data.get("memory_count"),
                )
            return ctx.strip()
        except Exception as e:
            logger.warning("Memory recall failed (non-fatal): %s", e)
            return ""

    def capture(
        self,
        user_content: str,
        assistant_content: str,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> bool:
        """记录一轮对话，供 L0→L1 管道提取。"""
        if not self.enabled or not self._capture_enabled:
            return False
        key = session_key or self._session_key
        user_content = _truncate(user_content, self._max_capture)
        assistant_content = _truncate(assistant_content, self._max_capture)
        if not user_content.strip() or not assistant_content.strip():
            return False
        body: dict[str, Any] = {
            "session_key": key,
            "user_content": user_content,
            "assistant_content": assistant_content,
        }
        if session_id:
            body["session_id"] = session_id
        try:
            data = self._post("/capture", body)
            logger.debug(
                "Memory capture ok: l0=%s notified=%s",
                (data or {}).get("l0_recorded"),
                (data or {}).get("scheduler_notified"),
            )
            return True
        except Exception as e:
            logger.warning("Memory capture failed (non-fatal): %s", e)
            return False

    def session_end(self, session_key: Optional[str] = None) -> None:
        """任务/会话结束，触发 flush。"""
        if not self.enabled:
            return
        key = session_key or self._session_key
        try:
            self._post("/session/end", {"session_key": key})
            logger.info("Memory session_end: %s", key)
        except Exception as e:
            logger.warning("Memory session_end failed (non-fatal): %s", e)

    def search_memories(self, query: str, limit: int = 5) -> str:
        if not self.enabled:
            return ""
        try:
            data = self._post("/search/memories", {"query": query, "limit": limit})
            return (data or {}).get("results") or ""
        except Exception as e:
            logger.warning("Memory search failed (non-fatal): %s", e)
            return ""

    def recall_for_task(self, task_id: str, requirement: str) -> str:
        """按任务需求召回；user 前缀带 task_id 便于追溯。"""
        query = f"[task:{task_id}] {requirement}"
        return self.recall(query)

    def capture_task_turn(
        self,
        task_id: str,
        user_content: str,
        assistant_content: str,
    ) -> bool:
        """写入带 task 标记的一轮对话。"""
        tagged_user = f"[task:{task_id}]\n{user_content}"
        return self.capture(tagged_user, assistant_content, session_id=task_id)

    def task_session_end(self, task_id: str) -> None:
        self.session_end(self._session_key)

    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{self._gateway_base}{path}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._gateway_base}{path}"
        payload = json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {e.code} {path}: {detail}") from e
        except URLError as e:
            raise RuntimeError(f"Gateway unreachable {url}: {e}") from e


def cfg_float_safe(path: str, default: float) -> float:
    try:
        from utils.config_loader import cfg_float
        return cfg_float(path, default)
    except (TypeError, ValueError):
        return default


def _truncate(text: str, max_chars: int) -> str:
    suffix = "\n\n…[truncated for memory capture]"
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


_singleton: Optional[TencentMemoryService] = None


def get_memory_service() -> TencentMemoryService:
    global _singleton
    if _singleton is None:
        _singleton = TencentMemoryService()
    return _singleton
