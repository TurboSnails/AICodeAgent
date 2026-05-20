#!/usr/bin/env python3
"""
从 figma-tools/configs/platform-figma-list.json 解析站点 → fileKey / cnName / enName。
用户只需提供站点（enName、中文简称或 cnName 全名），无需手填 FIGMA_FILE_KEY。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIGMA_TOOLS = PROJECT_ROOT / "figma-tools"
DEFAULT_PLATFORM_LIST = FIGMA_TOOLS / "configs" / "platform-figma-list.json"
DEFAULT_EN_MAP_PATHS = [
    FIGMA_TOOLS / "configs" / "haobo-style.json",
    FIGMA_TOOLS / "configs" / "platform-site-en-map.json",
]

QUERY_NORMALIZE = {
    "欧博": "殴博",
}


@dataclass
class PlatformSiteResolved:
    cn_name: str
    file_key: str
    en_name: Optional[str] = None
    node_id: Optional[str] = None
    kind: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.file_key) and not self.error

    def to_dict(self) -> dict:
        return {
            "cnName": self.cn_name,
            "fileKey": self.file_key,
            "enName": self.en_name,
            "nodeId": self.node_id,
            "kind": self.kind,
            "error": self.error,
        }


def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_file_key_to_en_map(en_map_paths: Optional[List[Path]] = None) -> Dict[str, dict]:
    paths = en_map_paths or DEFAULT_EN_MAP_PATHS
    mapping: Dict[str, dict] = {}
    for p in paths:
        raw = _load_json(p)
        if raw is None:
            continue
        rows = raw if isinstance(raw, list) else raw.get("sites", [])
        for row in rows or []:
            if row and row.get("fileKey") and row.get("enName"):
                mapping[row["fileKey"]] = {
                    "enName": row["enName"],
                    "nodeId": row.get("nodeId"),
                }
    return mapping


def load_platform_sites(platform_path: Optional[Path] = None) -> List[dict]:
    path = platform_path or DEFAULT_PLATFORM_LIST
    raw = _load_json(path)
    if not raw:
        return []
    sites = raw.get("sites", raw) if isinstance(raw, dict) else raw
    return _dedupe_by_file_key_prefer_design(sites or [])


def _dedupe_by_file_key_prefer_design(sites: List[dict]) -> List[dict]:
    by_key: Dict[str, dict] = {}
    for s in sites:
        k = s.get("fileKey")
        if not k:
            continue
        prev = by_key.get(k)
        if not prev:
            by_key[k] = s
            continue
        if prev.get("kind") != "design" and s.get("kind") == "design":
            by_key[k] = s
    return list(by_key.values())


def resolve_platform_site(
    query: str,
    platform_path: Optional[Path] = None,
    en_map_paths: Optional[List[Path]] = None,
) -> Optional[PlatformSiteResolved]:
    """
    解析站点标识，与 figma-tools resolvePlatformSite.js 规则一致：
    - fileKey（≥10 位字母数字）
    - enName（精确，不区分大小写）
    - cnName（精确）
    - cnName 包含（取最短匹配）
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return None

    q = QUERY_NORMALIZE.get(q_raw, q_raw)
    sites = load_platform_sites(platform_path)
    file_key_to_en = load_file_key_to_en_map(en_map_paths)
    q_lower_en = q_raw.lower()

    candidates: List[dict] = []

    if re.fullmatch(r"[a-zA-Z0-9]{10,}", q_raw) and any(s.get("fileKey") == q_raw for s in sites):
        candidates = [s for s in sites if s.get("fileKey") == q_raw]
    else:
        for row in sites:
            meta = file_key_to_en.get(row.get("fileKey", ""))
            if meta and meta["enName"].lower() == q_lower_en:
                candidates.append(row)
        if not candidates:
            candidates = [s for s in sites if s.get("cnName") == q]
        if not candidates:
            inc = [s for s in sites if s.get("cnName") and q in s["cnName"]]
            inc.sort(key=lambda s: len(s["cnName"]))
            candidates = inc

    if not candidates:
        return None

    row = candidates[0]
    file_key = row.get("fileKey", "")
    cn_name = row.get("cnName", "")
    meta = file_key_to_en.get(file_key)

    if not meta:
        return PlatformSiteResolved(
            cn_name=cn_name,
            file_key=file_key,
            en_name=None,
            node_id=None,
            kind=row.get("kind"),
            error="fileKey 无 enName：请在 platform-site-en-map.json 或 haobo-style.json 补全映射",
        )

    return PlatformSiteResolved(
        cn_name=cn_name,
        file_key=file_key,
        en_name=meta["enName"],
        node_id=meta.get("nodeId"),
        kind=row.get("kind"),
    )


def resolve_site_hint(site_hint: str) -> Optional[PlatformSiteResolved]:
    """统一入口：先 platform 列表，再 buildSrc Site.kt 兜底 cnName"""
    if not site_hint:
        return None

    resolved = resolve_platform_site(site_hint)
    if resolved:
        return resolved

    # 兜底：buildSrc site 文件名 → cnName，再用 cnName 查 platform 列表
    site_dir = PROJECT_ROOT / "buildSrc" / "src" / "main" / "kotlin" / "site"
    hint = site_hint.strip()
    for f in site_dir.glob("*.kt"):
        if f.stem in ("Site", "SiteChannels"):
            continue
        if hint.lower() not in f.stem.lower():
            continue
        content = f.read_text(encoding="utf-8")
        m = re.search(r'cnName\s*=\s*"([^"]+)"', content)
        if m:
            by_cn = resolve_platform_site(m.group(1))
            if by_cn:
                return by_cn
        m2 = re.search(r'enName\s*=\s*"([^"]+)"', content)
        if m2:
            by_en = resolve_platform_site(m2.group(1))
            if by_en:
                return by_en

    return None


def list_platform_sites_for_ui() -> List[dict]:
    """Web/Telegram 下拉：value 优先 enName，否则 cnName"""
    sites = load_platform_sites()
    file_key_to_en = load_file_key_to_en_map()
    options: List[dict] = []
    seen = set()

    for row in sorted(sites, key=lambda s: s.get("cnName", "")):
        fk = row.get("fileKey", "")
        cn = row.get("cnName", "")
        meta = file_key_to_en.get(fk, {})
        en = meta.get("enName", "")
        value = en or cn
        if not value or value in seen:
            continue
        seen.add(value)
        label = f"{cn} ({en})" if en else cn
        options.append({"value": value, "label": label, "cnName": cn, "enName": en, "fileKey": fk})

    return options


def write_platform_site_meta(workspace: Path, resolved: PlatformSiteResolved):
    (workspace / "platform_site.json").write_text(
        json.dumps(resolved.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
