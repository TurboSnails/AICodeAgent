#!/usr/bin/env python3
"""
Code Review Graph 桥接：为 Headless Agent 提供影响面 / 语义检索上下文。
优先 HTTP MCP（code-review-graph serve --http），失败则关键词回退。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from utils.logging_config import get_logger
from utils.config_loader import cfg_str, cfg_bool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = get_logger(__name__)
def _crg_http() -> str:
    return cfg_str("crg.http_url", "http://127.0.0.1:5555")


def _crg_repo() -> str:
    return cfg_str("crg.repo_root", str(PROJECT_ROOT))


def _http_mcp_tool_call(tool_name: str, arguments: dict) -> Optional[dict]:
    """Streamable HTTP MCP：调用单个 tool（需本地已启动 code-review-graph serve --http）"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    url = _crg_http().rstrip("/") + "/mcp"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if "result" in body:
                return body["result"]
            if "error" in body:
                logger.warning(f"CRG HTTP error: {body['error']}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning(f"CRG HTTP unavailable: {e}")
    return None


def semantic_search_files(query: str, limit: int = 8) -> List[str]:
    """语义检索相关 Kotlin 文件路径（相对 repo root）"""
    result = _http_mcp_tool_call(
        "semantic_search_nodes_tool",
        {
            "query": query,
            "kind": "File",
            "limit": limit,
            "repo_root": _crg_repo(),
            "detail_level": "minimal",
        },
    )
    paths: List[str] = []
    if result and isinstance(result, dict):
        content = result.get("content") or result
        if isinstance(content, list):
            for block in content:
                text = block.get("text", "") if isinstance(block, dict) else str(block)
                try:
                    data = json.loads(text)
                    for item in data.get("results", []):
                        fp = item.get("file_path") or item.get("name", "")
                        if fp.endswith(".kt"):
                            paths.append(_to_rel_path(fp))
                except json.JSONDecodeError:
                    pass
        elif isinstance(content, dict):
            for item in content.get("results", []):
                fp = item.get("file_path") or item.get("name", "")
                if fp.endswith(".kt"):
                    paths.append(_to_rel_path(fp))

    if paths:
        return paths[:limit]

    return _keyword_search_files(query, limit)


def get_impact_summary(changed_files: List[str], max_depth: int = 2) -> str:
    """获取变更影响面摘要文本"""
    if not changed_files:
        return ""
    rel_files = [_to_rel_path(f) for f in changed_files[:10]]
    result = _http_mcp_tool_call(
        "get_impact_radius_tool",
        {
            "changed_files": rel_files,
            "repo_root": _crg_repo(),
            "max_depth": max_depth,
            "detail_level": "minimal",
        },
    )
    if result:
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                try:
                    data = json.loads(text)
                    return data.get("summary", text[:2000])
                except json.JSONDecodeError:
                    return text[:2000]
            return json.dumps(result, ensure_ascii=False)[:2000]
    return _fallback_impact_hint(rel_files)


def extract_files_from_consensus(workspace: Path) -> List[str]:
    consensus = workspace / "consensus.md"
    if not consensus.exists():
        return []
    text = consensus.read_text(encoding="utf-8")
    found = re.findall(r"(app/[\w./-]+\.kt)", text)
    return list(dict.fromkeys(found))[:15]


def build_architect_graph_context(requirement: str, workspace: Path) -> str:
    """供 Architect Agent 使用：相关文件 + 影响面"""
    parts = ["## Code Graph Context (Auto-Retrieved)"]
    seeds = extract_files_from_consensus(workspace)
    if not seeds:
        seeds = semantic_search_files(requirement, limit=5)
    if seeds:
        parts.append("\n### 相关文件\n" + "\n".join(f"- `{p}`" for p in seeds))
        impact = get_impact_summary(seeds)
        if impact:
            parts.append(f"\n### 影响面摘要\n{impact}")
    else:
        parts.append("\n（图谱不可用，已回退关键词检索）")
    return "\n".join(parts) + "\n"


def _to_rel_path(path: str) -> str:
    p = path.replace("\\", "/")
    root = _crg_repo().replace("\\", "/").rstrip("/")
    if p.startswith(root + "/"):
        return p[len(root) + 1 :]
    if p.startswith("/"):
        idx = p.find("/app/")
        if idx >= 0:
            return p[idx + 1 :]
    return p


def _keyword_search_files(query: str, limit: int) -> List[str]:
    keywords = [w for w in re.split(r"\W+", query) if len(w) > 2][:5]
    if not keywords:
        return []
    java_root = PROJECT_ROOT / "app" / "src"
    hits: List[str] = []
    for kt in java_root.rglob("*.kt"):
        name = kt.name.lower()
        if any(kw.lower() in name for kw in keywords):
            hits.append(str(kt.relative_to(PROJECT_ROOT)).replace("\\", "/"))
        if len(hits) >= limit:
            break
    return hits


def _fallback_impact_hint(files: List[str]) -> str:
    lines = ["（图谱 HTTP 未连接，仅列出候选变更文件）"] + [f"- {f}" for f in files]
    return "\n".join(lines)


def crg_status() -> bool:
    """检查本地 CRG HTTP 是否可用"""
    try:
        req = urllib.request.Request(_crg_http().rstrip("/") + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


def ensure_crg_http_background() -> bool:
    """若未运行且本机有 CLI，尝试后台启动 serve --http（供 start.sh 调用）"""
    if crg_status():
        return True
    if not cfg_bool("crg.auto_start", False):
        return False
    port = _crg_http().split(":")[-1].rstrip("/")
    log = PROJECT_ROOT / "AICodeAgent" / "data" / "logs" / "crg_http.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "code-review-graph", "serve", "--http",
        "--repo", _crg_repo(),
        "--port", port,
    ]
    try:
        with open(log, "a", encoding="utf-8") as lf:
            subprocess.Popen(
                cmd, stdout=lf, stderr=lf, cwd=_crg_repo(),
                start_new_session=True,
            )
        return True
    except Exception as e:
        logger.error(f"CRG auto-start failed: {e}")
        return False
