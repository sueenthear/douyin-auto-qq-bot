# -*- coding: utf-8 -*-
"""协议端后端（NapCat / SnowLuma）适配的离线单测。

覆盖两件事：
  1. ``to_file_uri`` 按 RFC 8089 编码 —— 同时兼容两个后端
  2. ``NapCatClient.detect_backend`` 按 get_version_info 的 app_name 判定

两个后端的解析机制（已读源码 + 实测）：
  * NapCat   : ``decodeURIComponent(uri.slice(8))``
  * SnowLuma : Node ``fileURLToPath()``（走 new URL，未编码 # 会截断）
"""

import json
from pathlib import Path

import pytest

from qq_bot.napcat import NapCatClient, NapCatError, to_file_uri

# ---------------------------------------------------------------- URI 编码

def test_to_file_uri_plain_ascii_unchanged():
    """无特殊字符的路径：输出与朴素拼法完全一致（对 NapCat 无回归）。"""
    path = r"C:\temp\douyin_bot_ab\author_title_123.mp4"
    assert to_file_uri(path) == "file:///C:/temp/douyin_bot_ab/author_title_123.mp4"


def test_to_file_uri_encodes_hash_and_percent():
    """# 必须编码（否则 SnowLuma 当 fragment 截断）；% 必须编码（否则 URI malformed）。"""
    uri = to_file_uri(r"C:\t\d\眠眠羊毛衫_#厚黑_1.mp4")
    assert "#" not in uri
    assert "%23" in uri

    uri2 = to_file_uri(r"C:\t\d\100%纯棉_1.mp4")
    assert "%25" in uri2


def test_to_file_uri_encodes_non_ascii():
    """中文必须 UTF-8 百分号编码，SnowLuma 的 fileURLToPath 才能还原。"""
    uri = to_file_uri(r"C:\t\d\中文 空格_1.mp4")
    assert "%E4%B8%AD%E6%96%87" in uri     # 中文
    assert "%20" in uri                     # 空格


def test_to_file_uri_roundtrips_like_path_as_uri():
    """实现应与 pathlib.Path.as_uri() 等价。"""
    for raw in (r"C:\a\b.mp4", r"C:\目录\图 1.jpeg",
                r"C:\t\x#y%z_1.mp4", r"C:\t\中文_1.mp4"):
        assert to_file_uri(raw) == Path(raw).as_uri()


def test_to_file_uri_keeps_windows_drive_form():
    """必须是 file:///C:/ 形式（正斜杠），协议端以此判断本地文件。"""
    uri = to_file_uri(r"C:\a\b.mp4")
    assert uri.startswith("file:///C:/")
    assert "\\" not in uri


def test_to_file_uri_fallback_for_relative_path(monkeypatch):
    """无法转绝对 URI 时退回朴素拼法，不抛异常。"""
    uri = to_file_uri("relative/path.mp4")     # 相对路径 → as_uri 抛 ValueError
    assert uri.startswith("file:///")
    assert uri.endswith("relative/path.mp4")


# ---------------------------------------------------------------- 后端探测

class _FakeClient(NapCatClient):
    """不建立真实连接的客户端，只覆写 call()。"""

    def __init__(self, response):
        super().__init__("ws://127.0.0.1:1/", token="")
        self._response = response

    def call(self, action, timeout=None, **params):  # noqa: D102
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_detect_backend_snowluma():
    client = _FakeClient({"app_name": "SnowLuma", "app_version": "1.14.20-node",
                          "protocol_version": "v11"})
    assert client.detect_backend() == "snowluma"


def test_detect_backend_napcat():
    client = _FakeClient({"app_name": "NapCat", "app_version": "4.8.0"})
    assert client.detect_backend() == "napcat"


def test_detect_backend_is_case_insensitive():
    client = _FakeClient({"app_name": "SNOWLUMA-RUNTIME"})
    assert client.detect_backend() == "snowluma"


def test_detect_backend_returns_empty_on_unknown_app():
    """返回体没有可识别的 app_name 时返回空串，由调用方回退。"""
    assert _FakeClient({"app_version": "1.0"}).detect_backend() == ""
    assert _FakeClient({}).detect_backend() == ""
    assert _FakeClient(None).detect_backend() == ""


def test_detect_backend_swallows_napcat_error():
    """协议端不支持该 action 时不应抛出（旧版兼容）。"""
    client = _FakeClient(NapCatError("get_version_info 返回异常"))
    assert client.detect_backend() == ""


def test_detect_backend_ignores_non_dict_data():
    client = _FakeClient([1, 2, 3])
    assert client.detect_backend() == ""
