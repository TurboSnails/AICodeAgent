#!/usr/bin/env python3
"""
HTTP 请求日志中间件
记录所有进入 Gateway 的请求：方法、路径、IP、状态码、耗时

用法：
    from utils.request_logger import log_request

    class MyHandler:
        def do_GET(self):
            with log_request(self, logger) as ctx:
                # 处理请求
                ctx.status = 200
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Generator


class RequestContext:
    """请求上下文，用于在 with 块内设置状态码"""
    def __init__(self, method: str, path: str, client_ip: str):
        self.method = method
        self.path = path
        self.client_ip = client_ip
        self.status = 200
        self.start = time.time()

    @property
    def duration_ms(self) -> int:
        return int((time.time() - self.start) * 1000)


@contextmanager
def log_request(handler, logger) -> Generator[RequestContext, None, None]:
    """
    上下文管理器：自动记录请求开始和结束。

    使用方式：
        with log_request(self, logger) as ctx:
            # 处理逻辑
            ctx.status = 200  # 设置响应状态码
    """
    method = getattr(handler, "command", "UNKNOWN")
    path = getattr(handler, "path", "/")
    client_ip = handler.headers.get("X-Forwarded-For", "")
    if not client_ip:
        client_ip = getattr(handler, "client_address", ("-",))[0]

    ctx = RequestContext(method, path, client_ip)
    logger.info("[REQ] %s %s from %s", method, path, client_ip)

    try:
        yield ctx
    except Exception as e:
        ctx.status = 500
        logger.error("[REQ] %s %s -> %d in %dms | ERROR: %s", method, path, ctx.status, ctx.duration_ms, e)
        raise
    else:
        logger.info("[REQ] %s %s -> %d in %dms", method, path, ctx.status, ctx.duration_ms)
