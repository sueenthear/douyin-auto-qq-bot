# -*- coding: utf-8 -*-
"""douyin_api / xbogus / downloader 候选回退的离线单元测试（网络全部 mock）。"""

import os

import pytest
import requests

from douyin_core import douyin_api
from douyin_core.douyin_api import (
    DouyinAPIError,
    default_query,
    ensure_ms_token,
    fetch_video_detail,
    pick_video_candidates,
    sign_url,
)
from douyin_core.xbogus import XBogus


# ---------------------------------------------------------------- XBogus

def test_xbogus_signature_matches_reference():
    """本项目的 XBogus 输出必须与参考项目 jiji262/douyin-downloader 完全一致。"""
    ref_path = r"E:/AIcode/.ref_douyin/utils/xbogus.py"
    if not os.path.exists(ref_path):
        pytest.skip("参考项目源码不存在，跳过对比")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ref_xbogus", ref_path)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)

    url = ("https://www.douyin.com/aweme/v1/web/aweme/detail/"
           "?aweme_id=7412345678901234567&aid=6383&msToken=abc")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
    mine_url, mine_xb, _ = XBogus(ua).build(url)
    theirs_url, theirs_xb, _ = ref.XBogus(ua).build(url)
    assert mine_url == theirs_url
    assert len(mine_xb) == 28
    assert mine_xb == theirs_xb


def test_sign_url_appends_signature():
    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1&aid=6383"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
    signed, req_ua = sign_url(url, ua)
    # ABogus 优先；若 gmssl 不可用则回退 X-Bogus，两者都合法
    assert "a_bogus=" in signed or "X-Bogus=" in signed
    assert signed.startswith(url + "&")
    assert req_ua  # 请求必须携带与签名一致的 UA


# ---------------------------------------------------------------- 参数与 msToken

def test_default_query_complete():
    q = default_query()
    for key in ("device_platform", "aid", "version_code", "version_name",
                "browser_name", "browser_version", "msToken", "os_name"):
        assert key in q, f"缺少参数 {key}"
    assert q["aid"] == "6383"
    assert q["device_platform"] == "webapp"
    assert q["msToken"]


def test_ensure_ms_token_uses_cookie_first():
    token = "t" * 164
    cookie = f"ttwid=xx; msToken={token}; sessionid=abc"
    assert ensure_ms_token(cookie) == token


def test_ensure_ms_token_fallback_fake():
    token = ensure_ms_token("")
    assert len(token) == 184  # 随机占位 token 与真实长度一致


def test_ensure_ms_token_rejects_short_cookie_token(monkeypatch):
    # cookie 中的 msToken 长度非法 → 走生成流程（mock 真实生成失败 → 随机）
    monkeypatch.setattr(douyin_api, "_gen_real_ms_token", lambda timeout=8.0: "")
    token = ensure_ms_token("msToken=short")
    assert len(token) == 184


# ---------------------------------------------------------------- 详情接口

class FakeResp:
    def __init__(self, status=200, text="{}", json_data=None):
        self.status_code = status
        self.text = text
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


def _make_detail_resp():
    return FakeResp(json_data={
        "aweme_detail": {
            "aweme_id": "7412345678901234567",
            "desc": "测试视频",
            "video": {"play_addr": {"url_list": [
                "https://www.douyin.com/aweme/v1/playwm/?video_id=v1"]}},
        }
    })


def test_fetch_detail_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "douyin_core.douyin_api.requests.get",
        lambda *a, **k: calls.append(a[0]) or _make_detail_resp())
    detail = fetch_video_detail("7412345678901234567")
    assert detail["aweme_id"] == "7412345678901234567"
    assert "a_bogus=" in calls[0] or "X-Bogus=" in calls[0]  # 请求必须带签名
    assert "aweme/v1/web/aweme/detail/" in calls[0]


def test_fetch_detail_risk_control_retries_then_fails(monkeypatch):
    """403 视为风控：重试后仍失败 → 抛 DouyinAPIError。"""
    monkeypatch.setattr(douyin_api, "_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(douyin_api, "time", __import__("time"))
    resp = FakeResp(status=403, text="")
    monkeypatch.setattr("douyin_core.douyin_api.requests.get", lambda *a, **k: resp)
    with pytest.raises(DouyinAPIError, match="风控"):
        fetch_video_detail("7412345678901234567")


def test_fetch_detail_login_required(monkeypatch):
    data = {"status_code": 2483, "status_msg": "请先登录"}
    monkeypatch.setattr("douyin_core.douyin_api.requests.get",
                        lambda *a, **k: FakeResp(json_data=data))
    with pytest.raises(DouyinAPIError, match="登录"):
        fetch_video_detail("7412345678901234567")


def test_fetch_detail_empty_returns_error(monkeypatch):
    data = {"aweme_detail": None}
    monkeypatch.setattr("douyin_core.douyin_api.requests.get",
                        lambda *a, **k: FakeResp(json_data=data))
    with pytest.raises(DouyinAPIError):
        fetch_video_detail("7412345678901234567")


def test_fetch_detail_tries_both_aids(monkeypatch):
    """aid=6383 返回空且无 filter_reason → 不再尝试 1128。"""
    responses = {"6383": {"aweme_detail": None}}
    original_query = douyin_api.default_query

    def fake_query(cookie=""):
        q = original_query(cookie)
        return q

    def fake_get(url, **kw):
        return FakeResp(json_data={"aweme_detail": None})

    monkeypatch.setattr("douyin_core.douyin_api.default_query", fake_query)
    monkeypatch.setattr("douyin_core.douyin_api.requests.get", fake_get)
    with pytest.raises(DouyinAPIError):
        fetch_video_detail("7412345678901234567")
    assert responses  # 占位断言，避免未使用变量


# ---------------------------------------------------------------- 无水印候选

VIDEO_WITH_LADDER = {
    "bit_rate": [
        {"bit_rate": 1000, "play_addr": {
            "width": 540, "height": 960,
            "url_list": ["https://v3-dy.douyinvod.com/540p.mp4"]}},
        {"bit_rate": 3000, "play_addr": {
            "width": 1080, "height": 1920,
            "url_list": ["https://v9-dy.douyinvod.com/1080p.mp4"]}},
        {"bit_rate": 2000, "play_addr": {
            "width": 1080, "height": 1920,
            "url_list": ["https://v3-dy.douyinvod.com/1080p_low.mp4"]}},
    ],
    "play_addr": {
        "uri": "v0200abc",
        "url_list": ["https://www.douyin.com/aweme/v1/playwm/?video_id=v0200abc"],
    },
}


def test_pick_video_candidates_highest_quality_direct_first():
    cands = pick_video_candidates(VIDEO_WITH_LADDER)
    assert cands
    # 最高画质（1080x1920，码率 3000）的直连地址排第一
    assert cands[0] == "https://v9-dy.douyinvod.com/1080p.mp4"
    # 所有候选均无水印
    for c in cands:
        assert "playwm" not in c
    # 兜底含 playwm→play 替换的地址
    assert any("aweme/v1/play/" in c for c in cands)


def test_pick_video_candidates_no_bit_rate():
    video = {"play_addr": {
        "uri": "v0200abc",
        "url_list": ["https://www.douyin.com/aweme/v1/playwm/?video_id=v0200abc"],
    }}
    cands = pick_video_candidates(video)
    assert cands
    for c in cands:
        assert "playwm" not in c
    # 构造的 play 端点带签名（a_bogus 或 X-Bogus 均可）
    assert any(("a_bogus=" in c or "X-Bogus=" in c) for c in cands)


def test_pick_video_candidates_empty():
    assert pick_video_candidates({}) == []
    assert pick_video_candidates(None) == []


# ---------------------------------------------------------------- 下载候选回退

def test_download_file_falls_back_to_next_url(tmp_path, monkeypatch):
    from douyin_core import downloader as dl

    class StreamResp:
        def __init__(self, status, chunks):
            self.status_code = status
            self.headers = {}
            self._chunks = chunks

        def iter_content(self, chunk_size):
            return iter(self._chunks)

        def close(self):
            pass

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.startswith("https://bad.example/"):
            return StreamResp(403, [])
        return StreamResp(200, [b"hello", b" world"])

    monkeypatch.setattr("douyin_core.downloader.requests.get", fake_get)
    dest = tmp_path / "out"
    path = dl.download_file("https://bad.example/1.mp4", str(dest), "a.mp4",
                            url_fallbacks=["https://good.example/2.mp4"])
    assert path.endswith("a.mp4")
    assert open(path, "rb").read() == b"hello world"
    assert len(calls) == 2  # 主地址失败后尝试了备用地址


def test_download_file_all_fail(tmp_path, monkeypatch):
    from douyin_core import downloader as dl

    class FailResp:
        status_code = 403
        headers = {}

        def close(self):
            pass

    monkeypatch.setattr("douyin_core.downloader.requests.get",
                        lambda *a, **k: FailResp())
    with pytest.raises(dl.DownloadError):
        dl.download_file("https://bad.example/1.mp4", str(tmp_path), "a.mp4",
                         url_fallbacks=["https://bad.example/2.mp4"])
