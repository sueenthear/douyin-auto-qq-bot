# -*- coding: utf-8 -*-
"""login_manager 的离线单元测试（网络部分全部 mock）。"""

import time

import pytest
import requests

from douyin_core.login_manager import (
    CookieStore,
    LoginManager,
    LoginState,
    STATE_COLORS,
    STATE_LABELS,
    find_browser,
    has_session_cookie,
    verify_cookie_online,
)


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- Cookie 存取

def test_cookie_store_roundtrip(tmp_path):
    store = CookieStore(str(tmp_path / "cookies.json"))
    assert store.load() == ""
    store.save("sessionid=abc; ttwid=xyz")
    assert store.load() == "sessionid=abc; ttwid=xyz"
    store.clear()
    assert store.load() == ""


def test_cookie_store_ignore_corrupt(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("{bad json", encoding="utf-8")
    store = CookieStore(str(p))
    assert store.load() == ""


# ---------------------------------------------------------------- 会话判断

def test_has_session_cookie():
    assert has_session_cookie("ttwid=x; sessionid=abc123")
    assert has_session_cookie("sessionid_ss=abc; ttwid=x")
    assert has_session_cookie("SID_GUARD=abc")
    assert not has_session_cookie("ttwid=x; msToken=y; odin_tt=z")
    assert not has_session_cookie("")
    assert not has_session_cookie(None)


# ---------------------------------------------------------------- 在线验证

class FakeResp:
    def __init__(self, status, location="", payload=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("响应体不是 JSON")
        return self._payload


def test_verify_redirect_to_login_means_not_logged(monkeypatch):
    monkeypatch.setattr(
        "douyin_core.login_manager.requests.get",
        lambda *a, **k: FakeResp(302, "https://www.douyin.com/login/?next=/user/self"))
    assert verify_cookie_online("sessionid=abc") is False


def test_verify_logged_in_payload(monkeypatch):
    monkeypatch.setattr(
        "douyin_core.login_manager.requests.get",
        lambda *a, **k: FakeResp(
            200, payload={"status_code": 0, "user": {"uid": "123"}}))
    assert verify_cookie_online("sessionid=abc") is True


def test_verify_http_200_but_not_logged(monkeypatch):
    """未登录时接口同样返回 HTTP 200，仅凭状态码会误判为已登录（旧实现缺陷）。"""
    monkeypatch.setattr(
        "douyin_core.login_manager.requests.get",
        lambda *a, **k: FakeResp(
            200, payload={"status_code": 8, "status_msg": "用户未登录",
                          "user": None}))
    assert verify_cookie_online("sessionid=abc") is False


def test_verify_non_json_conservative(monkeypatch):
    """非 JSON 响应（风控页等）无法判断，保守放行，避免误清用户凭据。"""
    monkeypatch.setattr("douyin_core.login_manager.requests.get",
                        lambda *a, **k: FakeResp(200))
    assert verify_cookie_online("sessionid=abc") is True


def test_verify_network_error_conservative(monkeypatch):
    def boom(*a, **k):
        raise requests.RequestException("network down")
    monkeypatch.setattr("douyin_core.login_manager.requests.get", boom)
    assert verify_cookie_online("sessionid=abc") is True


# ---------------------------------------------------------------- 状态机

def test_check_no_cookie_not_logged(tmp_path):
    mgr = LoginManager(store=CookieStore(str(tmp_path / "c.json")))
    results = []
    mgr.check_login_async(lambda s, m: results.append((s, m)))
    assert _wait_for(lambda: mgr.state == LoginState.NOT_LOGGED)
    assert results[-1][0] == LoginState.NOT_LOGGED
    assert mgr.state == LoginState.NOT_LOGGED


def test_check_with_valid_cookie_logged_in(tmp_path, monkeypatch):
    store = CookieStore(str(tmp_path / "c.json"))
    store.save("sessionid=abc123")
    monkeypatch.setattr("douyin_core.login_manager.verify_cookie_online",
                        lambda cookie, timeout=15.0: True)
    mgr = LoginManager(store=store)
    results = []
    mgr.check_login_async(lambda s, m: results.append((s, m)))
    assert _wait_for(lambda: len(results) >= 2)  # CHECKING + 结果
    assert results[0][0] == LoginState.CHECKING
    assert results[-1][0] == LoginState.LOGGED_IN
    assert mgr.cookie == "sessionid=abc123"


def test_check_expired_cookie_not_logged(tmp_path, monkeypatch):
    store = CookieStore(str(tmp_path / "c.json"))
    store.save("sessionid=expired")
    monkeypatch.setattr("douyin_core.login_manager.verify_cookie_online",
                        lambda cookie, timeout=15.0: False)
    mgr = LoginManager(store=store)
    results = []
    mgr.check_login_async(lambda s, m: results.append((s, m)))
    assert _wait_for(lambda: len(results) >= 2)
    assert results[-1][0] == LoginState.NOT_LOGGED


# ---------------------------------------------------------------- 浏览器/界面数据

def test_find_browser():
    b = find_browser()
    assert b is None or (b[0] in ("edge", "chrome") and b[1])


def test_state_colors_and_labels_complete():
    for st in LoginState:
        assert st.value in STATE_COLORS
        assert st.value in STATE_LABELS
    # 四色要求：灰/黄/红/绿
    assert set(STATE_COLORS.values()) == {
        "#999999", "#f0ad4e", "#d9534f", "#5cb85c"}
