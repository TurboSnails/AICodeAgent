"""
asset_manager.py 单元测试
覆盖：资产命名、哈希计算、SVG→VectorDrawable、本地扫描、复用判断
"""
import sys
from pathlib import Path

import hashlib

import pytest

from services.asset_manager import (
    sanitize_asset_name,
    content_hash,
    _normalize_path_data,
    _svg_path_hash,
    _vector_path_hash,
    _icon_name_similarity,
    svg_to_vector_drawable,
    scan_local_drawable_dirs,
    find_reusable_local,
)

class TestSanitizeAssetName:
    def test_basic(self):
        assert sanitize_asset_name("My Icon") == "ic_my_icon"

    def test_already_ic_prefix(self):
        assert sanitize_asset_name("ic_settings") == "ic_settings"

    def test_img_prefix_preserved(self):
        assert sanitize_asset_name("img_banner") == "img_banner"

    def test_special_chars(self):
        assert sanitize_asset_name("Icon@2x!!") == "ic_icon_2x"

    def test_empty_fallback(self):
        # 空字符串 fallback 为 "figma_asset"，再自动加 "ic_" 前缀
        assert sanitize_asset_name("!!!") == "ic_figma_asset"

    def test_truncate(self):
        long_name = "a" * 100
        assert len(sanitize_asset_name(long_name)) <= 64

class TestContentHash:
    def test_deterministic(self):
        data = b"hello"
        assert content_hash(data) == content_hash(b"hello")
        assert content_hash(data) != content_hash(b"world")

class TestNormalizePathData:
    def test_lowercase_and_strip(self):
        assert _normalize_path_data("M 10 20 L 30 40") == "m1020l3040"

    def test_comma_handling(self):
        assert _normalize_path_data("M10,20L30,40") == "m1020l3040"

class TestSvgPathHash:
    def test_empty_svg(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        assert _svg_path_hash(svg) == ""

    def test_with_path(self):
        svg = b'<svg><path d="M0 0 L10 10"/></svg>'
        h1 = _svg_path_hash(svg)
        svg2 = b'<svg><path d="M 0 0 L 10 10"/></svg>'
        h2 = _svg_path_hash(svg2)
        assert h1 == h2

class TestVectorPathHash:
    def test_empty_vector(self):
        vec = b'<vector xmlns:android="http://schemas.android.com/apk/res/android"></vector>'
        assert _vector_path_hash(vec) == ""

    def test_with_path_data(self):
        vec = b'''<vector xmlns:android="http://schemas.android.com/apk/res/android">
            <path android:pathData="M0 0L10 10"/>
        </vector>'''
        h = _vector_path_hash(vec)
        assert len(h) == 16

class TestIconNameSimilarity:
    def test_exact_match(self):
        assert _icon_name_similarity("ic_delete", "ic_delete") == 1.0

    def test_substring(self):
        assert _icon_name_similarity("delete", "ic_delete_forever") == 0.85

    def test_no_match(self):
        assert _icon_name_similarity("abc", "xyz") < 0.5

class TestSvgToVectorDrawable:
    def test_simple_path(self):
        svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
            <path d="M0 0h24v24H0z" fill="none"/>
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>
        </svg>'''
        xml = svg_to_vector_drawable(svg, width=24, height=24)
        assert xml is not None
        assert 'android:width="24dp"' in xml
        assert 'android:viewportWidth="24.0"' in xml or 'android:viewportWidth="24"' in xml
        assert "<path" in xml

    def test_invalid_xml(self):
        assert svg_to_vector_drawable(b"not xml") is None

class TestScanLocalDrawableDirs:
    def test_empty_when_no_project(self, tmp_path: Path):
        # 传一个不存在的 site_hint，不应抛异常
        result = scan_local_drawable_dirs(site_hint="nonexistent")
        assert isinstance(result, dict)

class TestFindReusableLocal:
    def test_no_locals(self):
        result = find_reusable_local(b'<svg><path d="M0 0"/></svg>', {}, "ic_test")
        assert result is None

    def test_name_similarity_fallback(self, tmp_path: Path):
        d = tmp_path / "drawable"
        d.mkdir()
        f = d / "ic_settings.xml"
        f.write_bytes(b'<vector><path android:pathData="M0 0"/></vector>')
        local_map = {"ic_settings": f}
        # SVG 与 Vector 哈希不同，但名称相似度足够
        svg = b'<svg><path d="M1 1"/></svg>'
        result = find_reusable_local(svg, local_map, figma_name="ic_settings")
        assert result is not None
        assert result[0] == "ic_settings"