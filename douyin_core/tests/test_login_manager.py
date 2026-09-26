# -*- coding: utf-8 -*-
"""login_manager 的离线单元测试（网络部分全部 mock）。"""

import json
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


# ---------------------------------------------------------------- profile 占用

def test_find_profile_holders_returns_none_when_undetectable(monkeypatch):
    """检测不可用时返回 None（而非 []）。

    调用方据此区分「确认空闲」与「无法检测」—— 若把检测失败当成空闲，
    就会把「启动失败」误判成其它原因，正是本次要修的坑。
    """
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "")
    assert lm.find_profile_holders(r"C:\some\profile") is None


def test_find_profile_holders_parses_single_object(monkeypatch):
    """PowerShell 只返回一个对象时是 dict 而非 list，需兼容。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc(json.dumps({
                            "ProcessId": 1234, "Name": "msedge.exe",
                            "CommandLine":
                                'msedge.exe --user-data-dir=C:\\p\\profile '
                                '--test-type=webdriver'})))
    holders = lm.find_profile_holders(r"C:\p\profile")
    assert holders == [(1234, "msedge.exe", True, True)]


def test_find_profile_holders_filters_by_profile_dir(monkeypatch):
    """不得把 user-data-dir 指向别处的浏览器（用户自己开的）算作占用。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    payload = [
        {"ProcessId": 1, "Name": "msedge.exe",
         "CommandLine": 'msedge.exe --user-data-dir=C:\\p\\target'},
        {"ProcessId": 2, "Name": "msedge.exe",
         "CommandLine": 'msedge.exe --user-data-dir=C:\\other\\mine'},
    ]
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc(json.dumps(payload)))
    holders = lm.find_profile_holders(r"C:\p\target")
    assert [h[0] for h in holders] == [1]


def test_find_profile_holders_handles_backslash_and_quote_variance(monkeypatch):
    """命令行带引号 / 正斜杠时仍应命中（路径写法差异不能导致漏判）。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    payload = {"ProcessId": 7, "Name": "chrome.exe",
               "CommandLine": 'chrome.exe --user-data-dir="C:/p/Prof"'}
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc(json.dumps(payload)))
    holders = lm.find_profile_holders(r"C:\p\Prof")
    assert [h[0] for h in holders] == [7]


def test_find_profile_holders_detects_selenium_flag(monkeypatch):
    """识别 --test-type=webdriver，用于建议「可安全结束」。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    payload = [
        {"ProcessId": 1, "Name": "msedge.exe",
         "CommandLine": 'msedge.exe --user-data-dir=C:\\p\\t '
                        '--test-type=webdriver'},
        {"ProcessId": 2, "Name": "msedge.exe",
         "CommandLine": 'msedge.exe --type=renderer --user-data-dir=C:\\p\\t'},
    ]
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc(json.dumps(payload)))
    holders = {h[0]: h for h in lm.find_profile_holders(r"C:\p\t")}
    assert holders[1][2] is True       # selenium 主进程
    assert holders[1][3] is True       # 主进程
    assert holders[2][3] is False      # 渲染子进程


def test_find_profile_holders_empty_output_is_free(monkeypatch):
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc(""))
    assert lm.find_profile_holders(r"C:\p\t") == []


def test_find_profile_holders_bad_json_returns_none(monkeypatch):
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "_powershell_exe", lambda: "pwsh")
    monkeypatch.setattr(lm.subprocess, "run",
                        lambda *a, **k: _FakeProc("not json"))
    assert lm.find_profile_holders(r"C:\p\t") is None


def test_profile_lock_message_includes_actionable_command():
    from douyin_core import login_manager as lm
    holders = [(111, "msedge.exe", True, True),
               (222, "msedge.exe", True, False)]
    msg = lm._profile_lock_message(holders, r"C:\p\profile")
    assert "taskkill /F /PID 111" in msg       # 只对主进程给命令
    assert "222" in msg
    assert "独占锁" in msg


def test_profile_lock_message_without_selenium_suggests_closing_windows():
    from douyin_core import login_manager as lm
    holders = [(333, "chrome.exe", False, True)]
    msg = lm._profile_lock_message(holders, r"C:\p\profile")
    assert "taskkill" not in msg
    assert "关闭" in msg


# ---------------------------------------------------------------- 启动前检查

class _FakeDriver:
    def __init__(self, quit_raises=False):
        self.quit_calls = 0
        self._quit_raises = quit_raises

    def quit(self):
        self.quit_calls += 1
        if self._quit_raises:
            raise RuntimeError("已经退出了")


def test_launch_checked_raises_when_profile_locked(monkeypatch):
    """目录被占用时给出可执行提示，而不是含糊的 session not created。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "find_profile_holders",
                        lambda d: [(999, "msedge.exe", True, True)])
    launched = []
    monkeypatch.setattr(lm, "_launch_driver",
                        lambda k, d: launched.append(1))
    with pytest.raises(lm.LoginError, match="taskkill"):
        lm._launch_driver_checked("edge", r"C:\p\profile")
    assert launched == []          # 检测到占用就不该尝试启动


def test_launch_checked_diagnoses_after_failed_start(monkeypatch):
    """启动失败且复查发现被占用 → 转成占用提示。"""
    from douyin_core import login_manager as lm
    calls = {"n": 0}

    def holders(d):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [(5, "msedge.exe", True, True)]

    monkeypatch.setattr(lm, "find_profile_holders", holders)
    monkeypatch.setattr(
        lm, "_launch_driver",
        lambda k, d: (_ for _ in ()).throw(
            RuntimeError("session not created: Chrome instance exited")))
    with pytest.raises(lm.LoginError, match="占用"):
        lm._launch_driver_checked("edge", r"C:\p\profile")


def test_launch_checked_reports_plain_start_failure(monkeypatch):
    """未被占用但启动失败 → 给通用排查建议。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "find_profile_holders", lambda d: [])
    monkeypatch.setattr(
        lm, "_launch_driver",
        lambda k, d: (_ for _ in ()).throw(
            RuntimeError("session not created: Chrome instance exited")))
    with pytest.raises(lm.LoginError, match="浏览器启动失败"):
        lm._launch_driver_checked("edge", r"C:\p\profile")


def test_launch_checked_returns_driver_when_free(monkeypatch):
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "find_profile_holders", lambda d: [])
    sentinel = object()
    monkeypatch.setattr(lm, "_launch_driver", lambda k, d: sentinel)
    assert lm._launch_driver_checked("edge", r"C:\p\profile") is sentinel


def test_launch_checked_proceeds_when_detection_unavailable(monkeypatch):
    """检测不可用（None）时不应阻断启动 —— 但启动失败仍会复查。"""
    from douyin_core import login_manager as lm
    monkeypatch.setattr(lm, "find_profile_holders", lambda d: None)
    sentinel = object()
    monkeypatch.setattr(lm, "_launch_driver", lambda k, d: sentinel)
    assert lm._launch_driver_checked("edge", r"C:\p\profile") is sentinel


# ---------------------------------------------------------------- 窗口生命周期

def test_quit_driver_swallows_errors():
    """关闭失败不应影响主流程（Cookie 已拿到）。"""
    from douyin_core import login_manager as lm
    d = _FakeDriver(quit_raises=True)
    lm._quit_driver(d)             # 不抛异常
    assert d.quit_calls == 1


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
