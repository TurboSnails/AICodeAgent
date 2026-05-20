#!/usr/bin/env python3
"""
Visual Asset Manager — Figma 资产嗅探、哈希去重、入库、更新 asset_map.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SIMILARITY_THRESHOLD = float(os.environ.get("ASSET_HASH_SIMILARITY", "0.95"))


def content_hash(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _normalize_path_data(d: str) -> str:
    """归一化 SVG/Vector pathData：统一空格、小写、去除冗余"""
    if not d:
        return ""
    # 小写、去除多余空格、去除逗号前后空格
    d = d.lower().strip()
    d = re.sub(r"[\s,]+", " ", d)
    d = re.sub(r"(?<=[a-z])\s+(?=[-\d])", "", d)  # 命令和数字间不要空格
    d = re.sub(r"\s+", "", d)  # 最终去除所有空格
    return d


def _svg_path_hash(data: bytes) -> str:
    """从 SVG 提取 path data 并计算归一化哈希"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    paths = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "path" and elem.get("d"):
            paths.append(_normalize_path_data(elem.get("d")))
    if not paths:
        return ""
    # 排序后拼接，保证顺序无关
    combined = "|".join(sorted(paths))
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _vector_path_hash(data: bytes) -> str:
    """从 Android VectorDrawable 提取 pathData 并计算归一化哈希"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    ns = "http://schemas.android.com/apk/res/android"
    paths = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "path":
            d = elem.get(f"{{{ns}}}pathData") or elem.get("android:pathData")
            if d:
                paths.append(_normalize_path_data(d))
    if not paths:
        return ""
    combined = "|".join(sorted(paths))
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _icon_name_similarity(name1: str, name2: str) -> float:
    """基于名称的模糊相似度（0~1），用于 path 哈希未命中时的 fallback"""
    n1 = re.sub(r"[^a-z0-9]", "", name1.lower())
    n2 = re.sub(r"[^a-z0-9]", "", name2.lower())
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    if n1 in n2 or n2 in n1:
        return 0.85
    # Jaccard 相似度
    set1, set2 = set(n1), set(n2)
    inter = len(set1 & set2)
    union = len(set1 | set2)
    if union == 0:
        return 0.0
    return inter / union


def sanitize_asset_name(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", name.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "figma_asset"
    if not s.startswith("ic_") and not s.startswith("img_"):
        s = f"ic_{s}"
    return s[:64]


def scan_local_drawable_dirs(site_hint: str = "") -> Dict[str, Path]:
    """name(stem) -> absolute path"""
    mapping: Dict[str, Path] = {}
    dirs = [
        PROJECT_ROOT / "app" / "src" / "main" / "res",
    ]
    if site_hint:
        site_res = PROJECT_ROOT / "app" / "src" / "siteRes" / site_hint
        if site_res.exists():
            dirs.append(site_res)
    for base in dirs:
        for sub in ("drawable", "drawable-nodpi", "drawable-xxhdpi"):
            d = base / sub
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.suffix in (".xml", ".png", ".webp", ".svg"):
                    mapping.setdefault(f.stem, f)
    return mapping


def find_reusable_local(figma_bytes: bytes, local_map: Dict[str, Path], figma_name: str = "") -> Optional[Tuple[str, str]]:
    """
    基于 SVG path 感知哈希 + 名称相似度判断本地是否已有可复用图标。
    返回 (local_stem, reason) 或 None。
    """
    figma_hash = _svg_path_hash(figma_bytes)
    best_match: Optional[Tuple[str, float, str]] = None

    for stem, path in local_map.items():
        try:
            raw = path.read_bytes()
            if path.suffix == ".xml":
                local_hash = _vector_path_hash(raw)
            else:
                local_hash = _svg_path_hash(raw)
        except OSError:
            continue

        # 1. path 哈希完全匹配
        if figma_hash and local_hash and figma_hash == local_hash:
            return stem, f"SVG path 感知哈希完全一致 ({stem})"

        # 2. 名称相似度 fallback
        if figma_name:
            sim = _icon_name_similarity(figma_name, stem)
            if sim >= SIMILARITY_THRESHOLD and (best_match is None or sim > best_match[1]):
                best_match = (stem, sim, f"名称相似度 {sim:.0%} ({stem})")

    if best_match:
        return best_match[0], best_match[2]

    return None


def _parse_transform(transform: str) -> dict:
    """解析 SVG transform 属性，返回 {translate, scale, rotate} 字典"""
    result: dict = {}
    if not transform:
        return result
    # translate(x, y)
    m = re.search(r'translate\(\s*([^,)]+)(?:,\s*([^)]+))?\s*\)', transform)
    if m:
        result['translateX'] = m.group(1).strip()
        result['translateY'] = (m.group(2) or '0').strip()
    # scale(sx, sy)
    m = re.search(r'scale\(\s*([^,)]+)(?:,\s*([^)]+))?\s*\)', transform)
    if m:
        result['scaleX'] = m.group(1).strip()
        result['scaleY'] = (m.group(2) or m.group(1)).strip()
    # rotate(angle, cx, cy) — 忽略旋转中心，只取角度
    m = re.search(r'rotate\(\s*([^,)]+)', transform)
    if m:
        result['rotation'] = m.group(1).strip()
    return result


def _svg_elem_to_android(elem: ET.Element, parent_transform: dict = None) -> Optional[str]:
    """递归转换 SVG 元素为 VectorDrawable XML 片段"""
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
    transform = _parse_transform(elem.get("transform", ""))
    if parent_transform:
        transform = {**parent_transform, **transform}

    children_xml = []
    for child in elem:
        child_xml = _svg_elem_to_android(child, transform)
        if child_xml:
            children_xml.append(child_xml)

    if tag == "path":
        d = elem.get("d")
        if not d:
            return None
        attrs = [f'android:pathData="{d}"']
        # 颜色：优先使用主题属性，除非 SVG 明确指定了非黑色/非 currentColor
        svg_fill = elem.get("fill", "")
        if svg_fill and svg_fill.lower() not in ("none", "currentcolor", "#000000", "#000", "black"):
            attrs.append(f'android:fillColor="{svg_fill}"')
        else:
            attrs.append('android:fillColor="?attr/colorControlNormal"')
        # stroke
        stroke = elem.get("stroke")
        if stroke and stroke.lower() != "none":
            attrs.append(f'android:strokeColor="{stroke}"')
            stroke_w = elem.get("stroke-width", "1")
            attrs.append(f'android:strokeWidth="{stroke_w}"')
        # 如果有 transform，包装在 group 里
        if transform:
            g_attrs = []
            if 'translateX' in transform:
                g_attrs.append(f'android:translateX="{transform["translateX"]}"')
                g_attrs.append(f'android:translateY="{transform.get("translateY", "0")}"')
            if 'scaleX' in transform:
                g_attrs.append(f'android:scaleX="{transform["scaleX"]}"')
                g_attrs.append(f'android:scaleY="{transform["scaleY"]}"')
            if 'rotation' in transform:
                g_attrs.append(f'android:rotation="{transform["rotation"]}"')
            return f'<group {" ".join(g_attrs)}>\n            <path {" ".join(attrs)}/>\n        </group>'
        return f'<path {" ".join(attrs)}/>'

    if tag in ("g", "svg"):
        return "\n        ".join(children_xml) if children_xml else None

    return None


def svg_to_vector_drawable(svg_bytes: bytes, width: int = 24, height: int = 24) -> Optional[str]:
    """SVG → VectorDrawable：支持 viewBox、group transform、主题色"""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return None

    # 提取 viewBox
    viewbox = root.get("viewBox", "")
    if viewbox:
        parts = viewbox.replace(",", " ").split()
        if len(parts) >= 4:
            try:
                vx, vy, vw, vh = map(float, parts[:4])
                viewport_w = vw
                viewport_h = vh
            except ValueError:
                viewport_w = width
                viewport_h = height
        else:
            viewport_w = width
            viewport_h = height
    else:
        viewport_w = width
        viewport_h = height

    # 提取所有路径 / 组
    body_xml = _svg_elem_to_android(root)
    if not body_xml:
        return None

    return f'''<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="{width}dp"
    android:height="{height}dp"
    android:viewportWidth="{viewport_w}"
    android:viewportHeight="{viewport_h}">
    {body_xml}
</vector>
'''


def resolve_drawable_target_dir(site_hint: str = "") -> Path:
    if site_hint:
        d = PROJECT_ROOT / "app" / "src" / "siteRes" / site_hint / "drawable"
    else:
        d = PROJECT_ROOT / "app" / "src" / "main" / "res" / "drawable"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ingest_workspace_figma_files(
    workspace: Path,
    site_hint: str = "",
    dry_run: bool = False,
) -> List[dict]:
    """
    扫描 workspace/figma/assets 下 svg/png，去重后入库（不覆盖已有文件）。
    返回 asset_map entries。
    """
    assets_dir = workspace / "figma" / "assets"
    if not assets_dir.exists():
        return []

    local_map = scan_local_drawable_dirs(site_hint)
    target_dir = resolve_drawable_target_dir(site_hint)
    entries: List[dict] = []

    for f in sorted(assets_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".svg", ".png", ".webp"):
            continue
        raw = f.read_bytes()
        stem = sanitize_asset_name(f.stem)
        reuse = find_reusable_local(raw, local_map, figma_name=f.name)
        if reuse:
            local_stem, reason = reuse
            entries.append({
                "figma_node": f.name,
                "local_file": f"{local_stem}.xml" if local_map[local_stem].suffix == ".xml" else local_stem,
                "action": "reuse_local",
                "decision_reason": reason,
            })
            continue

        out_name = stem
        out_xml = target_dir / f"{out_name}.xml"
        if out_xml.exists():
            entries.append({
                "figma_node": f.name,
                "local_file": f"{out_name}.xml",
                "action": "reuse_local",
                "decision_reason": "目标 drawable 已存在，禁止覆盖",
            })
            continue

        if f.suffix.lower() == ".svg":
            vector = svg_to_vector_drawable(raw)
            if vector and not dry_run:
                out_xml.write_text(vector, encoding="utf-8")
                entries.append({
                    "figma_node": f.name,
                    "local_file": f"{out_name}.xml",
                    "action": "ingested_vector",
                    "decision_reason": "SVG 转 VectorDrawable 已入库",
                })
            else:
                entries.append({
                    "figma_node": f.name,
                    "local_file": None,
                    "action": "download_failed",
                    "decision_reason": "SVG 过于复杂，未能转换",
                })
        else:
            out_img = target_dir / f"{out_name}{f.suffix.lower()}"
            if not dry_run:
                shutil.copy2(f, out_img)
            entries.append({
                "figma_node": f.name,
                "local_file": f"{out_name}{f.suffix.lower()}",
                "action": "ingested_raster",
                "decision_reason": f"复制 {f.suffix} 至 siteRes/main drawable",
            })

    return entries


def fetch_figma_svg_via_api(
    node_ids: List[str],
    file_key: str,
    token: str,
    workspace: Path,
) -> List[dict]:
    """Figma REST：批量导出 SVG 到 workspace/figma/assets"""
    if not node_ids or not file_key or not token:
        return []
    ids_param = ",".join(node_ids[:20])
    url = (
        f"https://api.figma.com/v1/images/{file_key}"
        f"?ids={urllib.parse.quote(ids_param)}&format=svg"
    )
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[FIGMA API] {e}")
        return []

    images = data.get("images") or {}
    assets_dir = workspace / "figma" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for node_id, img_url in images.items():
        if not img_url:
            continue
        try:
            with urllib.request.urlopen(img_url, timeout=30) as r:
                svg_data = r.read()
            name = sanitize_asset_name(node_id.replace(":", "_"))
            out = assets_dir / f"{name}.svg"
            out.write_bytes(svg_data)
            downloaded.append({"node_id": node_id, "file": str(out.name)})
        except Exception as e:
            print(f"[FIGMA API] download {node_id}: {e}")

    return downloaded


def sniff_figma_icon_nodes(file_key: str, token: str, keywords: List[str]) -> List[str]:
    """从 Figma 文件 JSON 嗅探可能为图标的节点 id"""
    url = f"https://api.figma.com/v1/files/{file_key}?depth=2"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[FIGMA API] file fetch: {e}")
        return []

    ids: List[str] = []
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    def walk(node: dict):
        name = (node.get("name") or "").lower()
        ntype = node.get("type", "")
        if ntype in ("VECTOR", "COMPONENT", "INSTANCE", "FRAME", "GROUP"):
            if any(k in name for k in kw_lower) or name.startswith(("ic_", "icon", "img_")):
                nid = node.get("id")
                if nid:
                    ids.append(nid)
        for ch in node.get("children") or []:
            walk(ch)

    walk(doc.get("document") or {})
    return ids[:30]


def process_visual_assets(
    task_requirement: str,
    workspace: Path,
    site_hint: str = "",
    file_key: str = "",
    merge_existing_map: Optional[dict] = None,
) -> dict:
    """
    完整视觉资产流水线：API 嗅探（可选）→ workspace 入库 → 更新 asset_map.json
    file_key 优先来自 platform-figma-list（platform_site.json），其次环境变量。
    """
    token = os.environ.get("FIGMA_TOKEN", "")
    meta_path = workspace / "platform_site.json"
    if not file_key and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            file_key = meta.get("fileKey", "") or file_key
            if not site_hint:
                site_hint = meta.get("enName", "") or site_hint
        except json.JSONDecodeError:
            pass
    if not file_key:
        file_key = os.environ.get("FIGMA_FILE_KEY", "")

    if token and file_key:
        kws = [w for w in re.split(r"\W+", task_requirement) if len(w) > 2][:8]
        node_ids = sniff_figma_icon_nodes(file_key, token, kws)
        if node_ids:
            print(f"[ASSET] Figma API 嗅探到 {len(node_ids)} 个候选节点")
            fetch_figma_svg_via_api(node_ids, file_key, token, workspace)

    ingested = ingest_workspace_figma_files(workspace, site_hint=site_hint)
    asset_map = merge_existing_map or {"assets": []}
    existing_nodes = {a.get("figma_node") for a in asset_map.get("assets", [])}
    for entry in ingested:
        if entry.get("figma_node") not in existing_nodes:
            asset_map["assets"].append(entry)
            existing_nodes.add(entry.get("figma_node"))

    (workspace / "asset_map.json").write_text(
        json.dumps(asset_map, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[ASSET] asset_map 更新完成，共 {len(asset_map.get('assets', []))} 条")
    return asset_map
