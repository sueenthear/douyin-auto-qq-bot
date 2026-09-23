# -*- coding: utf-8 -*-
"""
douyin_api.py — 抖音网页版官方 Web API 客户端（requests 同步版）

参考实现思路来自 jiji262/douyin-downloader（MIT）与 F2（Apache-2.0）：
  * 完整浏览器环境参数（aid/version_code/screen 等）——缺失会被风控拒绝
  * msToken：优先使用 Cookie 中的；否则调 mssdk 接口生成；失败用随机占位
  * X-Bogus 签名（见 xbogus.py），签名与 User-Agent 绑定，请求必须同 UA
  * 详情接口 /aweme/v1/web/aweme/detail/ 依次尝试 aid=6383（视频/图文）与
    aid=1128（仅视频）；403/429/空响应视为风控，指数退避重试
  * 风控 / 需登录时抛 ``RiskControlError``（DouyinAPIError 子类），
    便于上层「刷新 Cookie 后重试」；其余失败抛 ``DouyinAPIError``
  * 无水印地址：bit_rate 阶梯取最高画质 → 直连 CDN 优先 → 签名 play 端点
    兜底 → playwm 替换最后兜底
"""

from __future__ import annotations

import json
import random
import string
import time
from typing import Any, Dict, List, Optional

import requests
from urllib.parse import urlencode

from .xbogus import XBogus

try:
    from .abogus import ABogus, BrowserFingerprintGenerator
except Exception:  # pragma: no cover - 可选依赖（gmssl）
    ABogus = None
    BrowserFingerprintGenerator = None

# ---------------------------------------------------------------- 常量

_BASE_URL = "https://www.douyin.com"

# 签名与 UA 绑定：全局固定 UA，保证签名、请求、下载三处一致
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
_USER_AGENTS = [_UA]

# detail 接口的 aid 候选：6383 覆盖视频+图文，1128 仅视频（互相补充）
_DETAIL_AID_CANDIDATES = ("6383", "1128")

_RISK_STATUSES = frozenset({403, 429})  # 风控状态码，可重试
_RETRY_DELAYS = (1, 2, 5)               # 指数退避

# mssdk msToken 生成配置（来自 F2 conf.yaml，运行时无需拉取外部配置）
_MSSDK_CONF: Dict[str, Any] = {
    "url": (
        "https://mssdk.bytedance.com/web/r/token?ms_appid=6383"
        "&msToken=T4bNG9W2rKF7hBNwaYssDErnJEobDAk641DFaOn4hcsfAM8slpbZeKPM4Ml4"
        "rhDQq18iY8nQ0JR3J87SLZtDiDqtZdZawfBjCWAgtolQsoEtG6MLETvo4fwr7F28zGJ"
        "UFDdJgKEZHibNR0QshVBv28ygsQsJDzerKAtsgj9Pn5WsxyS1vfkiX3I%3D"
    ),
    "magic": 538969122,
    "version": 1,
    "dataType": 8,
    "ulr": 0,
    "strData": (
        "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"
        "IoV7B3nKBuAuF7p0uF9MwDpyu="
    ),
}


class DouyinAPIError(Exception):
    """API 调用失败（风控 / 未登录 / 数据缺失）。"""


class RiskControlError(DouyinAPIError):
    """风控 / 登录失效 —— 刷新 Cookie 后重试可能恢复。

    涵盖：403 / 429 / 空响应（风控拦截）、「需要登录后才能查看」等。
    与普通 DouyinAPIError 区分开，便于上层自动重登重试。
    """


# ---------------------------------------------------------------- msToken


def _gen_fake_ms_token() -> str:
    """随机占位 msToken（长度 184，与真实 token 一致）。"""
    return "".join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(182)) + "=="


def _is_valid_ms_token(token: str) -> bool:
    return len(token.strip()) in (164, 184)


def _gen_real_ms_token(timeout: float = 8.0) -> str:
    """调用 mssdk 接口生成真实 msToken；失败返回空串。"""
    conf = _MSSDK_CONF
    payload = {
        "magic": conf["magic"],
        "version": conf["version"],
        "dataType": conf["dataType"],
        "strData": conf["strData"],
        "ulr": conf["ulr"],
        "tspFromClient": int(time.time() * 1000),
    }
    try:
        resp = requests.post(
            conf["url"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
        )
        # requests 的 headers 只保留最后一个 Set-Cookie，需从原始响应读取全部
        set_cookies = []
        try:
            set_cookies = resp.raw.headers.getlist("Set-Cookie") or []
        except (AttributeError, KeyError):
            set_cookies = [v for v in resp.headers.get("Set-Cookie", "").split(";")]
        for header in set_cookies:
            for part in str(header).split(";"):
                if part.strip().startswith("msToken="):
                    token = part.split("=", 1)[1].strip()
                    if _is_valid_ms_token(token):
                        return token
    except (requests.RequestException, AttributeError):
        pass
    return ""


def ensure_ms_token(cookie: str = "") -> str:
    """返回可用的 msToken：Cookie 中的 → mssdk 生成 → 随机占位。"""
    for item in cookie.split(";"):
        item = item.strip()
        if item.startswith("msToken="):
            token = item.split("=", 1)[1].strip()
            if _is_valid_ms_token(token):
                return token
    real = _gen_real_ms_token()
    if real:
        return real
    return _gen_fake_ms_token()


# ---------------------------------------------------------------- 参数与签名


def default_query(cookie: str = "") -> Dict[str, str]:
    """构造与浏览器一致的请求参数（风控校验的重要部分）。"""
    query = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "139.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "139.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "support_h265": "1",
        "support_dash": "1",
        "uifid": "",
        "msToken": ensure_ms_token(cookie),
    }
    return query


def sign_url(url: str, user_agent: str = _UA) -> tuple:
    """给 URL 追加签名（a_bogus 优先，X-Bogus 兜底）。

    :return: (签名后的完整 URL, 请求应使用的 User-Agent)
    """
    base, sep, query = url.partition("?")
    if ABogus is not None:
        try:
            fp = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
            signer = ABogus(fp=fp, user_agent=user_agent)
            params_with_ab, _ab, ab_ua, _body = signer.generate_abogus(query, "")
            return f"{base}?{params_with_ab}", ab_ua
        except Exception:
            pass  # 回退 X-Bogus
    signed, _, _ = XBogus(user_agent).build(url)
    return signed, user_agent


# ---------------------------------------------------------------- ttwid

_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_BODY = (
    '{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com",'
    '"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https",'
    '"union":true}'
)
_ttwid_cache: str = ""


def _extract_cookie_value(cookie: str, name: str) -> str:
    for item in (cookie or "").split(";"):
        item = item.strip()
        if item.startswith(name + "="):
            return item.split("=", 1)[1].strip()
    return ""


def ensure_ttwid(cookie: str = "") -> str:
    """返回可用的 ttwid：Cookie 中的 → 匿名注册（模块级缓存）。"""
    global _ttwid_cache
    existing = _extract_cookie_value(cookie, "ttwid")
    if existing:
        return existing
    if _ttwid_cache:
        return _ttwid_cache
    try:
        resp = requests.post(
            _TTWID_REGISTER_URL,
            data=_TTWID_BODY,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10,
        )
        ttwid = resp.cookies.get("ttwid") or ""
        if ttwid:
            _ttwid_cache = ttwid
            return ttwid
    except requests.RequestException:
        pass
    return ""


# ---------------------------------------------------------------- 详情接口


def fetch_video_detail(
    item_id: str,
    cookie: str = "",
    timeout: float = 15.0,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """调用 /aweme/v1/web/aweme/detail/ 获取作品详情。

    依次尝试 aid=6383 / 1128；403/429/空响应视为风控，指数退避重试。
    :raises DouyinAPIError: 全部尝试失败
    """
    user_agent = _UA
    headers = {
        "User-Agent": user_agent,
        "Referer": f"{_BASE_URL}/?recommend=1",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie
    else:
        # 无 Cookie 时自动注册匿名 ttwid（未登录访问的关键凭证）
        ttwid = ensure_ttwid()
        if ttwid:
            headers["Cookie"] = f"ttwid={ttwid}"
    cookie = headers.get("Cookie", "")

    for aid in _DETAIL_AID_CANDIDATES:
        params = default_query(cookie)
        params.update({"aweme_id": item_id, "aid": aid})
        query = urlencode(params)
        url = f"{_BASE_URL}/aweme/v1/web/aweme/detail/?{query}"
        signed_url, req_ua = sign_url(url, user_agent)
        headers["User-Agent"] = req_ua

        for attempt in range(max_retries):
            try:
                resp = requests.get(signed_url, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                raise DouyinAPIError(f"详情请求失败：{e}")

            if resp.status_code in _RISK_STATUSES or not resp.text:
                if attempt < max_retries - 1:
                    time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                    continue
                raise RiskControlError(
                    f"接口风控（HTTP {resp.status_code}），请稍后重试或登录后重试")
            if resp.status_code != 200:
                raise DouyinAPIError(f"详情接口 HTTP {resp.status_code}")

            try:
                data = resp.json()
            except ValueError:
                raise DouyinAPIError("详情接口返回非 JSON 数据")

            detail = data.get("aweme_detail") if isinstance(data, dict) else None
            if detail:
                return detail

            # 返回了数据但 aweme_detail 为空：可能是内容被过滤（图集/合集等）
            filter_info = data.get("filter_detail")
            reason = ""
            if isinstance(filter_info, dict):
                reason = str(filter_info.get("filter_reason") or "")
            if reason:
                break  # 换下一个 aid
            # status_code 2483 / 未登录提示
            if str(data.get("status_code")) == "2483" or "请先登录" in str(
                    data.get("status_msg") or ""):
                raise RiskControlError("需要登录后才能查看该作品，请先登录")
            break

    raise DouyinAPIError("未能获取视频详情（作品可能已删除、私密或需要登录）")


# ---------------------------------------------------------------- 无水印地址


def _extract_urls(source: Any) -> List[str]:
    if isinstance(source, dict):
        url_list = source.get("url_list") or source.get("urlList")
        if isinstance(url_list, list):
            return [u for u in url_list if isinstance(u, str) and u]
    elif isinstance(source, list):
        return [u for u in source if isinstance(u, str) and u]
    elif isinstance(source, str) and source:
        return [source]
    return []


def _is_watermarked(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in ("playwm", "watermark=1", "tplv-dy-water",
                                  "dy-water", "owner_watermark"))


def _pick_best_bit_rate_addr(video: Dict[str, Any]) -> Optional[str]:
    """从 video.bit_rate 阶梯中选最高画质（分辨率优先、码率决胜）的 URL。"""
    bit_rates = video.get("bit_rate") or []
    best_url = ""
    best_key = (-1, -1)
    for entry in bit_rates:
        if not isinstance(entry, dict):
            continue
        play_addr = entry.get("play_addr")
        if not isinstance(play_addr, dict):
            continue
        try:
            width = int(play_addr.get("width") or entry.get("width") or 0)
            height = int(play_addr.get("height") or entry.get("height") or 0)
            bit_rate = int(entry.get("bit_rate") or 0)
        except (TypeError, ValueError):
            continue
        pixels = width * height if width > 0 and height > 0 else 0
        key = (pixels, bit_rate)
        if key > best_key:
            for u in _extract_urls(play_addr):
                if u:
                    best_url = u
                    best_key = key
                    break
    return best_url or None


def _build_signed_play_url(video: Dict[str, Any]) -> Optional[str]:
    """用 video_id(uri) 构造签名 play 端点（watermark=0 无水印）。"""
    video_id = (video.get("play_addr") or {}).get("uri") or video.get("vid")
    if not video_id:
        return None
    params = {
        "video_id": video_id,
        "ratio": "1080p",
        "line": "0",
        "is_play_url": "1",
        "watermark": "0",
        "source": "PackSourceEnum_PUBLISH",
    }
    url = f"{_BASE_URL}/aweme/v1/play/?{urlencode(params)}"
    signed, _ = sign_url(url)
    return signed


def pick_video_candidates(video: Dict[str, Any]) -> List[str]:
    """按优先级返回可尝试的视频地址列表（全部无水印）。

    顺序：bit_rate 最高画质直连 CDN（非 douyin.com）→ 无水印 douyin.com
    play 端点（签名）→ 构造的签名 play 端点 → video.play_addr 去水印。
    """
    if not isinstance(video, dict):
        return []
    candidates: List[str] = []
    seen = set()

    def add(url: str, sign: bool = False):
        if not url or url in seen:
            return
        seen.add(url)
        if sign and "X-Bogus=" not in url and "a_bogus=" not in url:
            url, _ = sign_url(url)
        candidates.append(url)

    # 1. bit_rate 最高画质档
    best = _pick_best_bit_rate_addr(video)
    if best:
        if "douyin.com" in best:
            # play 端点，需要签名（去水印由服务端 watermark 参数保证）
            add(best, sign=True)
        else:
            add(best)
    # 2. play_addr 列表：仅取无水印地址（douyin.com play 端点需签名）
    for u in _extract_urls(video.get("play_addr")):
        if _is_watermarked(u):
            continue
        if "douyin.com" in u:
            add(u, sign=True)
        else:
            add(u)
    # 3. 构造签名 play 端点
    constructed = _build_signed_play_url(video)
    add(constructed)
    # 4. 兜底：playwm → play（带水印地址替换）
    for u in _extract_urls(video.get("play_addr")):
        if "playwm" in u:
            add(u.replace("playwm", "play", 1))
    return candidates
