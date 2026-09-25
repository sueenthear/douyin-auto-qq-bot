# -*- coding: utf-8 -*-
"""防风控措施的单测：请求节流、代理、msToken 缓存（全部离线）。"""

import time

import pytest

from douyin_core import douyin_api


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后恢复默认，避免相互影响。"""
    douyin_api.configure_rate_limit(1.0, 0.5)
    douyin_api.configure_proxy("")
    douyin_api._ms_token_cache = ("", 0.0)
    douyin_api._last_request_at = 0.0
    yield
    douyin_api.configure_rate_limit(1.0, 0.5)
    douyin_api.configure_proxy("")
    douyin_api._ms_token_cache = ("", 0.0)
    douyin_api._last_request_at = 0.0


# ---------------------------------------------------------------- 请求节流

def test_throttle_waits_after_first_request(monkeypatch):
    """连续两次 throttle：第二次必须等待约 min_interval。"""
    slept = []
    monkeypatch.setattr("douyin_core.douyin_api.time.sleep",
                        lambda s: slept.append(s))
    douyin_api.configure_rate_limit(1.0, 0.0)      # 无抖动，便于断言
    douyin_api.throttle()                          # 第一次不等待
    douyin_api.throttle()                          # 第二次应等待 ~1s
    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.0, abs=0.05)


def test_throttle_adds_jitter():
    """开启抖动时，等待时间应落在 [interval, interval*1.5] 内。"""
    douyin_api.configure_rate_limit(1.0, 0.5)
    douyin_api._last_request_at = time.time()
    waits = [douyin_api.throttle() for _ in range(5)]
    for w in waits:
        assert 0.0 <= w <= 1.5 + 0.05


def test_throttle_disabled_when_interval_zero(monkeypatch):
    """min_interval=0 时不等待。"""
    slept = []
    monkeypatch.setattr("douyin_core.douyin_api.time.sleep",
                        lambda s: slept.append(s))
    douyin_api.configure_rate_limit(0.0, 0.0)
    douyin_api.throttle()
    douyin_api.throttle()
    assert slept == []


def test_configure_rate_limit_clamps_negative():
    douyin_api.configure_rate_limit(-5.0, -1.0)
    assert douyin_api._min_interval == 0.0
    assert douyin_api._jitter_ratio == 0.0


def test_throttle_updates_last_request_at():
    douyin_api.configure_rate_limit(0.0, 0.0)
    before = douyin_api._last_request_at
    douyin_api.throttle()
    assert douyin_api._last_request_at >= before


# ---------------------------------------------------------------- 代理

def test_proxy_disabled_by_default():
    douyin_api.configure_proxy("")
    assert douyin_api._proxies() is None


def test_proxy_configured():
    douyin_api.configure_proxy("http://127.0.0.1:7890")
    p = douyin_api._proxies()
    assert p == {"http": "http://127.0.0.1:7890",
                 "https": "http://127.0.0.1:7890"}


def test_proxy_strips_whitespace():
    douyin_api.configure_proxy("  http://127.0.0.1:7890  ")
    assert douyin_api._proxy_url == "http://127.0.0.1:7890"


def test_proxy_none_means_direct():
    douyin_api.configure_proxy(None)
    assert douyin_api._proxies() is None


# ---------------------------------------------------------------- msToken 缓存

def test_ms_token_from_cookie_takes_priority():
    """Cookie 里有 msToken 时直接用，不查缓存也不打接口。"""
    token = "x" * 184
    assert douyin_api.ensure_ms_token(f"msToken={token}; ttwid=abc") == token


def test_ms_token_cached_avoids_repeated_requests(monkeypatch):
    """关键：第二次调用不应再打 mssdk 接口（否则每个作品都多一次请求）。"""
    calls = {"n": 0}

    def fake_gen(timeout=8.0):
        calls["n"] += 1
        return ""

    monkeypatch.setattr("douyin_core.douyin_api._gen_real_ms_token", fake_gen)
    first = douyin_api.ensure_ms_token("")
    second = douyin_api.ensure_ms_token("")
    assert calls["n"] == 1              # 只打了一次
    assert first == second              # 命中缓存，token 相同


def test_ms_token_cache_expires(monkeypatch):
    """缓存过期后应重新生成。"""
    calls = {"n": 0}

    def fake_gen(timeout=8.0):
        calls["n"] += 1
        return ""

    monkeypatch.setattr("douyin_core.douyin_api._gen_real_ms_token", fake_gen)
    douyin_api.ensure_ms_token("")
    douyin_api._ms_token_cache = (douyin_api._ms_token_cache[0],
                                  time.time() - douyin_api._MS_TOKEN_TTL - 1)
    douyin_api.ensure_ms_token("")
    assert calls["n"] == 2


def test_ms_token_fake_is_valid_length():
    token = douyin_api._gen_fake_ms_token()
    assert douyin_api._is_valid_ms_token(token)


# ---------------------------------------------------------------- 详情请求接入

def test_fetch_detail_applies_throttle(monkeypatch):
    """fetch_video_detail 每次真实请求前都应调用 throttle()。"""
    throttled = []
    monkeypatch.setattr("douyin_core.douyin_api.throttle",
                        lambda: throttled.append(1) or 0.0)

    class FakeResp:
        status_code = 200
        text = '{"aweme_detail": {"aweme_id": "1"}}'

        def json(self):
            return {"aweme_detail": {"aweme_id": "1"}}

    monkeypatch.setattr("douyin_core.douyin_api.requests.get",
                        lambda *a, **k: FakeResp())
    detail = douyin_api.fetch_video_detail("1", cookie="sessionid=x")
    assert detail["aweme_id"] == "1"
    assert len(throttled) == 1


def test_fetch_detail_passes_proxy(monkeypatch):
    """配置了代理后，请求应带上 proxies 参数。"""
    douyin_api.configure_proxy("http://127.0.0.1:7890")
    seen = {}

    class FakeResp:
        status_code = 200
        text = '{"aweme_detail": {"aweme_id": "1"}}'

        def json(self):
            return {"aweme_detail": {"aweme_id": "1"}}

    def fake_get(url, **kw):
        seen["proxies"] = kw.get("proxies")
        return FakeResp()

    monkeypatch.setattr("douyin_core.douyin_api.requests.get", fake_get)
    douyin_api.fetch_video_detail("1", cookie="sessionid=x")
    assert seen["proxies"] == {"http": "http://127.0.0.1:7890",
                               "https": "http://127.0.0.1:7890"}
