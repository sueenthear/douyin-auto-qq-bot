# -*- coding: utf-8 -*-
"""风控自动重登重试流程的离线单测（不联网、不启浏览器）。"""

import pytest

from douyin_core import DouyinAPIError, RiskControlError
from douyin_core.douyin_parser import ParseError, VideoInfo

from qq_bot.handler import DEFAULT_RISK_MESSAGE, DouyinQQBot

URL = "https://v.douyin.com/D5JLehTukGs/"
SHARE = f"4.10 复制打开抖音，看看【CatQ啾啾的作品】魔女之夜 {URL} Ehb:/ 04/05"


class FakeConfig:
    cookie_refresh_timeout = 60.0
    keep_files = False
    download_dir = "downloads"

    def __init__(self):
        self.messages = {
            "video": "检测到抖音视频分享链接，正在解析中……",
            "image": "检测到抖音图文分享链接，正在解析中……",
            "unknown": "检测到抖音分享链接，正在解析中……",
            "error": "解析失败：{error}",
            "risk": DEFAULT_RISK_MESSAGE,
        }

    def is_watched(self, *a):
        return True


class FakeParser:
    """按预设脚本依次返回结果或抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def parse_text(self, text):
        self.calls += 1
        item = self.script.pop(0) if self.script else ParseError("no script")
        if isinstance(item, Exception):
            raise item
        return item

    def set_cookie(self, cookie):
        self.cookie = cookie


def make_bot(script, refresh_ok=True):
    cfg = FakeConfig()
    bot = DouyinQQBot.__new__(DouyinQQBot)          # 跳过 __init__（不连 NapCat）
    bot.config = cfg
    bot.log = lambda m: None
    bot.parser = FakeParser(script)
    bot.stats = {"received": 0, "video": 0, "image": 0, "failed": 0,
                 "risk_refresh": 0}
    bot.refreshed = 0

    def fake_refresh():
        bot.refreshed += 1
        if refresh_ok:
            bot.parser.set_cookie("sessionid=new")
            bot.stats["risk_refresh"] += 1
            return True
        return False

    bot._refresh_cookie = fake_refresh
    return bot


# ---------------------------------------------------------------- 正常路径

def test_normal_parse_no_refresh():
    info = VideoInfo(item_id="1", title="魔女之夜", play_url="http://x")
    bot = make_bot([info])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is info and err == ""
    assert bot.refreshed == 0          # 未触发风控 → 不刷新 Cookie


# ---------------------------------------------------------------- 风控重试成功

def test_risk_control_refresh_then_success():
    info = VideoInfo(item_id="1", title="魔女之夜", play_url="http://x")
    bot = make_bot([RiskControlError("接口风控（HTTP 403）"), info])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is info and err == ""
    assert bot.refreshed == 1          # 只刷新一次
    assert bot.parser.calls == 2       # 重试一次


def test_risk_control_does_not_leak_error_message():
    """风控首次失败时不应产生报错文案（由调用方决定是否发送）。"""
    info = VideoInfo(item_id="1", play_url="http://x")
    bot = make_bot([RiskControlError("403"), info])
    _, err = bot._parse_with_risk_retry(SHARE)
    assert err == ""


# ---------------------------------------------------------------- 风控仍失败

def test_risk_control_persists_reports_with_link():
    bot = make_bot([RiskControlError("403"),
                    RiskControlError("接口风控（HTTP 403）")])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is None
    assert "风控" in err
    assert URL in err                  # 报错必须带触发链接
    assert bot.refreshed == 1          # 只尝试一次


def test_risk_refresh_failure_reports_with_link():
    bot = make_bot([RiskControlError("403")], refresh_ok=False)
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is None
    assert "刷新 Cookie 失败" in err
    assert URL in err
    assert bot.refreshed == 1


def test_risk_message_includes_reason_and_url():
    bot = make_bot([])
    text = bot._risk_message(URL, "接口风控（HTTP 403）")
    assert "接口风控（HTTP 403）" in text
    assert URL in text


def test_risk_message_handles_missing_url():
    bot = make_bot([])
    text = bot._risk_message("", "403")
    assert "未能提取链接" in text


# ---------------------------------------------------------------- 风控不被吞

def test_risk_control_propagates_through_fetch_info(monkeypatch):
    """回归：_fetch_info 曾用 except DouyinAPIError 吞掉风控，导致上层收不到信号。"""
    from douyin_core import DouyinParser, douyin_api

    def boom(item_id, cookie="", **kw):
        raise douyin_api.RiskControlError("接口风控（HTTP 403）")

    monkeypatch.setattr("douyin_core.douyin_parser.douyin_api.fetch_video_detail",
                        boom)
    parser = DouyinParser()
    with pytest.raises(RiskControlError):
        parser._fetch_info("123")


def test_risk_control_propagates_through_parse_url(monkeypatch):
    """端到端（URL 形式）：parse_url 必须把风控异常交给调用方。

    用 www.douyin.com 网页链接而非短链，避免 parse_text 跟随短链重定向
    时产生真实网络请求（那会让本测试依赖网络）。
    """
    from douyin_core import DouyinParser, douyin_api

    monkeypatch.setattr("douyin_core.douyin_parser.douyin_api.fetch_video_detail",
                        lambda item_id, cookie="", **kw: (_ for _ in ()).throw(
                            douyin_api.RiskControlError("403")))
    parser = DouyinParser()
    with pytest.raises(RiskControlError):
        parser.parse_url("https://www.douyin.com/video/7412345678901234567")


def test_risk_control_propagates_through_parse_text(monkeypatch):
    """端到端（分享文案）：短链重定向也 mock 掉，保证测试完全离线。"""
    from douyin_core import DouyinParser, douyin_api

    monkeypatch.setattr("douyin_core.douyin_parser.douyin_api.fetch_video_detail",
                        lambda item_id, cookie="", **kw: (_ for _ in ()).throw(
                            douyin_api.RiskControlError("403")))
    parser = DouyinParser()
    monkeypatch.setattr(parser, "_resolve_item_id",
                        lambda url: "7687183893074139874")
    with pytest.raises(RiskControlError):
        parser.parse_text(SHARE)


# ---------------------------------------------------------------- 其他异常

def test_non_risk_parse_error_uses_error_template():
    bot = make_bot([ParseError("作品已删除")])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is None
    assert err == "解析失败：作品已删除"
    assert bot.refreshed == 0          # 非风控不刷新


def test_unexpected_exception_is_wrapped():
    bot = make_bot([ValueError("boom")])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is None
    assert "ValueError" in err and "boom" in err
    assert bot.refreshed == 0


def test_risk_after_refresh_but_other_error():
    """刷新后遇到非风控错误 → 走普通 error 模板。"""
    bot = make_bot([RiskControlError("403"), ParseError("作品已删除")])
    got, err = bot._parse_with_risk_retry(SHARE)
    assert got is None
    assert err == "解析失败：作品已删除"


# ---------------------------------------------------------------- 异常层级

def test_risk_control_error_is_douyin_api_error():
    assert issubclass(RiskControlError, DouyinAPIError)
    # 能被 except DouyinAPIError 捕获（兼容既有回退逻辑）
    try:
        raise RiskControlError("403")
    except DouyinAPIError:
        pass
