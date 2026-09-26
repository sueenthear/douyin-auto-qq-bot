# -*- coding: utf-8 -*-
"""反检测升级的离线单测（网络全部 mock）。

覆盖四项升级：
  1. UA 池：启动随机选一次后全程固定
  2. 指纹自洽：query 屏幕参数取自签名指纹，而非写死值
  3. Argus 门禁：确定性 403 不重试
  4. msToken 配置：远程拉取失败时回退内置快照 + 失败退避

背景见 douyin_api 模块顶部注释与 README「防风控」一节。
"""

import pytest

from douyin_core import douyin_api


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """每个用例前后重置全局 profile / 缓存，避免相互影响。"""
    def reset():
        douyin_api._UA_FIXED = False
        douyin_api._BROWSER_FP = ""
        douyin_api._FP_GEOMETRY = {}
        douyin_api._ms_conf_cache = ({}, 0.0)
        douyin_api._ms_conf_retry_after = 0.0
        douyin_api._ms_token_cache = ("", 0.0)

    reset()
    yield
    reset()


# ---------------------------------------------------------------- UA 固定

def test_ua_selected_from_pool():
    douyin_api._ensure_profile()
    assert douyin_api._UA in douyin_api._UA_POOL


def test_ua_is_stable_across_calls():
    """关键：UA 一旦选定必须全程固定（签名与请求头才能一致）。"""
    first = douyin_api.current_user_agent()
    for _ in range(10):
        assert douyin_api.current_user_agent() == first
    assert douyin_api._UA == first


def test_set_user_agent_overrides_and_resets_fingerprint():
    douyin_api._ensure_profile()
    before_fp = douyin_api._BROWSER_FP
    custom = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestUA/1.0"
    assert douyin_api.set_user_agent(custom) == custom
    assert douyin_api.current_user_agent() == custom
    # 指纹应随之重新生成（不沿用旧 UA 的指纹）
    assert douyin_api._BROWSER_FP != before_fp or before_fp == ""


def test_ua_pool_has_no_duplicates():
    assert len(set(douyin_api._UA_POOL)) == len(douyin_api._UA_POOL)


# ---------------------------------------------------------------- 指纹自洽

def test_query_geometry_matches_signature_fingerprint():
    """核心：query 的 screen 参数必须与签名指纹同源。

    旧实现写死 1536x864，而签名指纹每次随机，两者互相矛盾 —— 真实浏览器
    不可能出现这种组合，会成为识别脚本的特征。
    """
    douyin_api._ensure_profile()
    if not douyin_api._BROWSER_FP:
        pytest.skip("无 gmssl，指纹不可用")
    query = douyin_api.default_query("")
    parts = douyin_api._BROWSER_FP.split("|")
    assert query["screen_width"] == parts[8]
    assert query["screen_height"] == parts[9]


def test_query_platform_matches_fingerprint():
    douyin_api._ensure_profile()
    if not douyin_api._BROWSER_FP:
        pytest.skip("无 gmssl，指纹不可用")
    query = douyin_api.default_query("")
    assert query["browser_platform"] == douyin_api._BROWSER_FP.split("|")[16]


def test_query_geometry_is_stable_across_calls():
    """指纹只生成一次，query 也就必须稳定（不能每请求换一套硬件参数）。"""
    first = douyin_api.default_query("")
    for _ in range(3):
        again = douyin_api.default_query("")
        assert again["screen_width"] == first["screen_width"]
        assert again["screen_height"] == first["screen_height"]


def test_fingerprint_stays_stable():
    douyin_api._ensure_profile()
    fp = douyin_api._BROWSER_FP
    for _ in range(3):
        douyin_api._ensure_profile()
        assert douyin_api._BROWSER_FP == fp


def test_parse_fp_geometry_handles_malformed():
    assert douyin_api._parse_fp_geometry("") == {}
    assert douyin_api._parse_fp_geometry("1|2|3") == {}
    geom = douyin_api._parse_fp_geometry("10|20|30|40|0|0|0|0|50|60|70|80|10|20|24|24|Win32")
    assert geom == {"inner_width": 10, "inner_height": 20,
                    "screen_width": 50, "screen_height": 60}


def test_query_falls_back_when_fingerprint_unavailable(monkeypatch):
    """无指纹（如缺 gmssl）时退回写死值，保证参数完整不缺失。"""
    monkeypatch.setattr(douyin_api, "_BROWSER_FP", "")
    monkeypatch.setattr(douyin_api, "_FP_GEOMETRY", {})
    monkeypatch.setattr(douyin_api, "_UA_FIXED", True)
    query = douyin_api.default_query("")
    assert query["screen_width"] == "1536"
    assert query["screen_height"] == "864"


# ---------------------------------------------------------------- 签名一致性

def test_signature_ua_equals_request_ua():
    """签名内嵌 UA 必须与返回的请求头 UA 一致（否则自相矛盾）。"""
    signed, ua = douyin_api.sign_url(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1&aid=6383")
    assert ua == douyin_api.current_user_agent()
    assert "a_bogus=" in signed or "X-Bogus=" in signed


def test_signature_uses_fixed_fingerprint():
    """签名应复用固定指纹，而非每次重新随机。"""
    douyin_api._ensure_profile()
    douyin_api.sign_url("https://www.douyin.com/aweme/v1/web/aweme/detail/?a=1")
    fp = douyin_api._BROWSER_FP
    douyin_api.sign_url("https://www.douyin.com/aweme/v1/web/aweme/detail/?a=2")
    assert douyin_api._BROWSER_FP == fp


# ---------------------------------------------------------------- Argus 门禁

def test_is_argus_rejection_detects_uifid():
    assert douyin_api._is_argus_rejection(
        403, "Blocked by ArgusSecurityPlugin Uifid Not Found")


def test_is_argus_rejection_detects_signature():
    assert douyin_api._is_argus_rejection(
        403, "Blocked by ArgusSecurityPlugin Signature Not Found")


def test_is_argus_rejection_ignores_plain_403():
    """普通 403（无 Argus 标记）是频率风控，应当重试。"""
    assert not douyin_api._is_argus_rejection(403, "rate limited")
    assert not douyin_api._is_argus_rejection(403, "")


def test_is_argus_rejection_requires_403():
    assert not douyin_api._is_argus_rejection(
        500, "Blocked by ArgusSecurityPlugin Uifid Not Found")


def test_argus_marker_extracts_reason():
    assert douyin_api._argus_marker(
        "Blocked by ArgusSecurityPlugin Uifid Not Found") == "Uifid Not Found"
    assert douyin_api._argus_marker("") == "ArgusSecurityPlugin"


def test_argus_rejection_is_not_retried(monkeypatch):
    """关键：Argus 确定性拒绝时应立即上抛，不做退避重试。

    重试只会白耗请求并加速触发验证码。
    """
    calls = []

    class FakeResp:
        status_code = 403
        text = "Blocked by ArgusSecurityPlugin Uifid Not Found"

        def json(self):
            return {}

    def fake_get(*a, **kw):
        calls.append(1)
        return FakeResp()

    slept = []
    # 隔离 msToken 配置/TTWID 的请求，只统计抖音详情请求
    monkeypatch.setattr(douyin_api, "_UA_FIXED", True)
    monkeypatch.setattr(douyin_api, "_ms_token_cache", ("x" * 184, 1e18))
    monkeypatch.setattr(douyin_api, "_ttwid_cache", "t" * 20)
    monkeypatch.setattr("douyin_core.douyin_api.requests.get", fake_get)
    monkeypatch.setattr("douyin_core.douyin_api.time.sleep",
                        lambda s: slept.append(s))
    monkeypatch.setattr("douyin_core.douyin_api.throttle", lambda: 0.0)

    with pytest.raises(douyin_api.RiskControlError, match="Uifid Not Found"):
        douyin_api.fetch_video_detail("1", cookie="sessionid=x", max_retries=3)
    assert len(calls) == 1        # 只发一次，未重试
    assert slept == []            # 未退避


def test_plain_403_is_still_retried(monkeypatch):
    """无 Argus 标记的 403 仍按原逻辑退避重试。"""
    calls = []

    class FakeResp:
        status_code = 403
        text = "rate limited"

        def json(self):
            return {}

    def fake_get(*a, **kw):
        calls.append(1)
        return FakeResp()

    slept = []
    # 只统计抖音详情请求：把 msToken 配置/TTWID 的请求与缓存隔离掉
    monkeypatch.setattr(douyin_api, "_UA_FIXED", True)
    monkeypatch.setattr(douyin_api, "_ms_token_cache", ("x" * 184, 1e18))
    monkeypatch.setattr(douyin_api, "_ttwid_cache", "t" * 20)
    monkeypatch.setattr("douyin_core.douyin_api.requests.get", fake_get)
    monkeypatch.setattr("douyin_core.douyin_api.time.sleep",
                        lambda s: slept.append(s))
    monkeypatch.setattr("douyin_core.douyin_api.throttle", lambda: 0.0)

    with pytest.raises(douyin_api.RiskControlError):
        douyin_api.fetch_video_detail("1", cookie="sessionid=x", max_retries=3)
    # 仅第一个 aid 就走满 3 次尝试（第 3 次判定失败后抛错，不再换 aid）
    assert len(calls) == 3
    assert len(slept) == 2        # 中间退避两次


# ---------------------------------------------------------------- msToken 配置

def test_fetch_remote_ms_conf_parses_yaml(monkeypatch):
    yaml_text = (
        "f2:\n"
        "  douyin:\n"
        "    msToken:\n"
        "      url: 'https://example.com/token'\n"
        "      magic: 123456\n"
        "      version: 1\n"
        "      dataType: 8\n"
        "      ulr: 0\n"
        "      strData: 'abc'\n"
    )

    class FakeResp:
        status_code = 200
        text = yaml_text

    monkeypatch.setattr("douyin_core.douyin_api.requests.get",
                        lambda *a, **kw: FakeResp())
    conf = douyin_api._fetch_remote_ms_conf()
    assert conf["magic"] == 123456
    assert conf["url"] == "https://example.com/token"


def test_fetch_remote_ms_conf_rejects_incomplete(monkeypatch):
    class FakeResp:
        status_code = 200
        text = "f2:\n  douyin:\n    msToken:\n      magic: 1\n"

    monkeypatch.setattr("douyin_core.douyin_api.requests.get",
                        lambda *a, **kw: FakeResp())
    assert douyin_api._fetch_remote_ms_conf() == {}


def test_fetch_remote_ms_conf_handles_network_error(monkeypatch):
    def boom(*a, **kw):
        raise douyin_api.requests.RequestException("离线")

    monkeypatch.setattr("douyin_core.douyin_api.requests.get", boom)
    assert douyin_api._fetch_remote_ms_conf() == {}


def test_load_ms_conf_falls_back_to_bundled(monkeypatch):
    """远程失败时回退内置快照，保证 mssdk 仍能拿到配置。"""
    monkeypatch.setattr(douyin_api, "_fetch_remote_ms_conf", lambda timeout=5.0: {})
    conf = douyin_api._load_ms_token_conf()
    assert conf is douyin_api._MSSDK_CONF


def test_load_ms_conf_caches_success(monkeypatch):
    calls = []

    def fake_fetch(timeout=5.0):
        calls.append(1)
        return {"url": "u", "magic": 1, "version": 1,
                "dataType": 8, "ulr": 0, "strData": "s"}

    monkeypatch.setattr(douyin_api, "_fetch_remote_ms_conf", fake_fetch)
    douyin_api._load_ms_token_conf()
    douyin_api._load_ms_token_conf()
    assert len(calls) == 1        # 第二次命中缓存


def test_load_ms_conf_backs_off_after_failure(monkeypatch):
    """连续失败不应每次都付超时代价（退避窗口内直接返回快照）。"""
    calls = []

    def fake_fetch(timeout=5.0):
        calls.append(1)
        return {}

    monkeypatch.setattr(douyin_api, "_fetch_remote_ms_conf", fake_fetch)
    first = douyin_api._load_ms_token_conf()
    second = douyin_api._load_ms_token_conf()
    assert first is douyin_api._MSSDK_CONF
    assert second is douyin_api._MSSDK_CONF
    assert len(calls) == 1        # 第二次在退避窗口内，未再请求


def test_parse_f2_conf_without_pyyaml_fallback(monkeypatch):
    """无 PyYAML 时用缩进兜底解析。"""
    yaml_text = (
        "f2:\n"
        "  douyin:\n"
        "    msToken:\n"
        "      url: https://example.com/t\n"
        "      magic: 42\n"
        "      version: 1\n"
        "      dataType: 8\n"
        "      ulr: 0\n"
        "      strData: xyz\n"
    )
    # 让 import yaml 失败，强制走兜底分支
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    conf = douyin_api._parse_f2_conf(yaml_text)
    assert conf["magic"] == 42
    assert conf["strData"] == "xyz"
