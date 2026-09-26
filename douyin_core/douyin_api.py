# -*- coding: utf-8 -*-
"""
douyin_api.py — 抖音网页版官方 Web API 客户端（requests 同步版）

参考实现思路来自 jiji262/douyin-downloader（MIT）与 F2（Apache-2.0）：
  * 完整浏览器环境参数（aid/version_code/screen 等）——缺失会被风控拒绝；
    **屏幕/平台等参数取自签名指纹本身**，保证与 a_bogus 自洽
  * UA 池：首次使用随机选定后全程固定（签名与请求头必须同 UA）
  * msToken：Cookie 中的 → 缓存 → 远程 F2 配置生成 → 内置快照 → 随机占位
  * a_bogus 签名（见 abogus.py）优先，X-Bogus 兜底；签名与 UA 绑定
  * 详情接口 /aweme/v1/web/aweme/detail/ 依次尝试 aid=6383 与 aid=1128
  * 风控 / 需登录时抛 ``RiskControlError``（DouyinAPIError 子类），
    便于上层「刷新 Cookie 后重试」；其余失败抛 ``DouyinAPIError``
  * **Argus 门禁**（2026-08 起）返回 403 ``Blocked by ArgusSecurityPlugin``，
    属确定性拒绝 —— 不再退避重试，并以 ``permanent=True`` 通知上层跳过重登
  * 无水印地址：bit_rate 阶梯取最高画质 → 直连 CDN 优先 → 签名 play 端点
    兜底 → playwm 替换最后兜底
"""

from __future__ import annotations

import json
import random
import string
import threading
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

# UA 池：启动（首次使用时）随机选一个，之后**全程固定**。
#
# 为什么必须固定而不能每请求轮换：a_bogus 签名把 UA 作为输入参与加密
# （abogus.py: rc4_encrypt(ua_key, user_agent)），签名与请求头 UA 必须严格
# 一致；同时该 UA 还决定浏览器指纹里的平台。若每请求换 UA，就会出现
# 「签名用 A、请求头用 B」的自相矛盾，反而更容易被 Argus 判定为脚本。
#
# 参考 jiji262/douyin-downloader：它在客户端初始化时 random.choice 选一次
# 并贯穿生命周期，本项目采用同一策略。
_UA_POOL = (
    # Windows / Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    # macOS / Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
)

# 当前固定 UA：首次使用时从池中随机选定，之后不变（可用 set_user_agent 覆盖）
_UA = _UA_POOL[0]
_UA_LOCK = threading.Lock()
_UA_FIXED = False

# 签名指纹：与 UA 同源、同样全程固定。
#
# 关键：query 里的 screen_width/screen_height 等硬件参数必须与签名指纹
# **来自同一次生成**，否则两者互相矛盾（如 query 写 1536x864、指纹随机出
# 1881x876），与真实浏览器的自洽性相悖，会成为识别脚本的特征。
# 参考项目即用 BrowserFingerprintGenerator 生成一次后复用。
_BROWSER_FP = ""
# 从指纹串解析出的屏幕/窗口尺寸，供 default_query 使用
_FP_GEOMETRY: Dict[str, int] = {}


def _fp_platform(ua: str) -> str:
    """按 UA 推断 navigator.platform（指纹末段）。"""
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "MacIntel"
    return "Win32"


def _parse_fp_geometry(fp: str) -> Dict[str, int]:
    """解析指纹串里的几何值，供 query 参数复用。

    指纹段序（abogus.BrowserFingerprintGenerator._generate_fingerprint）::

        0 innerW | 1 innerH | 2 outerW | 3 outerH | 4 screenX | 5 screenY
        6 0 | 7 0 | 8 screenW | 9 screenH | 10 availW | 11 availH
        12 innerW | 13 innerH | 14 24 | 15 24 | 16 platform
    """
    parts = fp.split("|")
    if len(parts) < 16:
        return {}
    try:
        return {
            "inner_width": int(parts[0]),
            "inner_height": int(parts[1]),
            "screen_width": int(parts[8]),
            "screen_height": int(parts[9]),
        }
    except (TypeError, ValueError):
        return {}


def _ensure_profile(preserve_ua: bool = False) -> None:
    """首次调用时固定 UA 与浏览器指纹。线程安全、幂等。

    :param preserve_ua: True 时保留当前 ``_UA``（由 set_user_agent 显式指定），
                        只重算指纹。
    """
    global _UA, _UA_FIXED, _BROWSER_FP, _FP_GEOMETRY
    if _UA_FIXED:
        return
    with _UA_LOCK:
        if _UA_FIXED:
            return
        if not preserve_ua:
            _UA = random.choice(_UA_POOL)
        # 指纹与 UA 必须同源：先按 UA 定平台，再生成对应指纹
        if BrowserFingerprintGenerator is not None:
            try:
                _BROWSER_FP = BrowserFingerprintGenerator.generate_fingerprint(
                    "Chrome" if "Chrome/" in _UA else "Edge")
            except Exception:
                _BROWSER_FP = ""
        # Chrome/macOS 的指纹平台需与 UA 一致
        if _BROWSER_FP and "Macintosh" in _UA:
            _BROWSER_FP = _BROWSER_FP.rsplit("|", 1)[0] + "|MacIntel"
        _FP_GEOMETRY = _parse_fp_geometry(_BROWSER_FP)
        _UA_FIXED = True


def set_user_agent(ua: str = "") -> str:
    """显式指定/重置固定 UA；传空串则重新随机选定。返回生效的 UA。

    会连带按新 UA 重新生成指纹，保证「签名 UA == 请求头 UA」且指纹与之一致。
    """
    global _UA, _UA_FIXED, _BROWSER_FP, _FP_GEOMETRY
    chosen = str(ua or "").strip() or random.choice(_UA_POOL)
    with _UA_LOCK:
        _UA = chosen
        _BROWSER_FP = ""
        _FP_GEOMETRY = {}
        _UA_FIXED = False          # 触发下面的指纹重算
    _ensure_profile(preserve_ua=True)
    return _UA


def current_user_agent() -> str:
    """当前固定 UA（会触发首次初始化）。"""
    _ensure_profile()
    return _UA


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
        "fW15xyeivmE5JAQZdb83gdUCCHlGDBZDeWxqYklwOYciPisi772aWHSG75OFvFZ5zS5R"
        "lfrFGzxNzRQllBoIw2wXT5VvEO9UzRqLMD2kh96/p8aCc56JCdvtz6oZx/j9vRUiy5"
        "Hdy4OGKqH7e0VqjP2biY6Zi27XiuWv6ZJ/owedPUULhR2LmyhLRAm6wZA3zRj6z6Xi"
        "ZQU64oWdAorw2Q03RCFp7AF9WPmXdgRDCQl/33NPthRL/TBLdJkEFtRLBmY29phw0W"
        "qI6dt6JdKEK+5Sdj7DdJj0ckrqCL0MJcdnyD1Ww5ZSCafBK0xMRhHQ3o39AfD6t0D5"
        "O4CtrpULW0+fWG755BnIAZnfmsc2SSxV+KwZKWY61Zx/MNju+S6TOKmDbL5w61ceR"
        "yTTCNeDmPxAJdp8qmsZnJrczwKgze71YMq3DeZdfg7cf9/RwqroB8TRilvCcLk63r"
        "/FLuGUr2+5Y7fA3KiiYNwhYJFzH/6T4Jo8R7Jy5QcBDa7loP4Q0uqYzP09BskRAwiZ"
        "cg+iZrdC1aJ06zfUxcUi7Q+EtA2S0Z6kGIanoqEfx+va2rIOBIZEn6+Bv2hGmPMwM"
        "0trm96KYCvATPdwdVEowKzuuajJFwic78mD+V3tIHlVWeXDqtNm2bRP+9nY9ZvS/f"
        "l7UuCbJLYxIekN77btrzKs/rrzCpoRoHvOuIDeXWBusLiJIU2ooa1AkXHitRoVcX57"
        "NJAYxb6G+w8V04B4EphNcBL6Xl/wD7EvIjHf7vIUqbcc70xh2CD+ZmufsFBTTa5bO"
        "Koj6SJDay3ni8V98n2ZGXKMSj415Mx3VNe/EuxDKUOCpksLmGW6hoK8K0H6QqiNPC"
        "seSZ3Cv3iuF0yILlTEiHWwkbyUwujwqi09ZznmoVyV5M9fdAIZ72EgEdpuTt/kh6D"
        "FGJ0Y9UdYih46SncUuYQCazLRTlkXlTAZ7q0/RhAdaR0zZzdhu1yHLJbK/upR9jFU"
        "I+5rOpjio6Y29cXGHX3i9lea/K0SocQLGa8jSg1AYG6rlVfhdYbPCQ8X53mmf1C+J"
        "OJaZTBnUoKXSev5xxotTeWruWLq3JrKxXQxEOYEsNS+zbUT/C4/Mfwop9IQ1FlRMP"
        "MvE0azbZI/Cmh3TIkXQRV6B/Yj8O+dBYINuHPXjyQ8A0648fXCjom3mnbl9Anr4K2"
        "h0o9MZ7WHDd+ZPi892QBvt1xZcDCq3v8pe9VqUY6uQoe4ex0xKMoA03ETfw5x9c+o"
        "w5/BC1Lpxjp0liCKt/6wJ165jA63FMSLRAkn1n61hrpesBzd5eFpPpN7NB2itqcTP"
        "usSFyj2YBdpTxYjFnh++E1vHFQvktJIwqjwY99l0ySSVm8Xs+IjK1DQc5frXnQnJy"
        "axXhFmitDHFoKQiJd/6XZIbC1gt+Hi/4j1LzijCb4kWGf5sFLz7I0eZdQJHquoIZ7"
        "hdNz/qlrTEH1UBitF7sRv8PbErg063C8anB2UBQsUKIRfKufgVhmneuSqBVUS2P3X"
        "kDFlJ043kZ4awB2F3mp/G1g7xr3RiM/OUKippXiJbB9WSDGaWsCl8er7lSpVWQKnd"
        "aIS64jJ/vyqQC2EB8prWFtVCyBlTTVm+VVSOeQ3n8x3PYhVhPLAlzhleApNr3PNWZ"
        "OcPWD16wVQ6s/PXcPzHVomUO5EmUC7L3JrNclYxG4iEtHS+GO8FOIPVfc8W8gTvBv"
        "hLl8dLX0OsNjXLOMwKvixcr6kUBwnjo0Nn3b7kX80ew/7xnr64evN1KtYRFNWrfah"
        "vjfhvyTcoKrzr7dlzI8QpsPG4MdggWzODaRfQhM/B3Awo6ezWGj5K87eMtAheL6b2"
        "hZZdvKGDRVoTBI8ebpYh9oUlPARwkhanW1B86Gpi04UAdJVrJ2S6TWhq+/dX8udhh"
        "DuDsxwyc0qfjTdjUzNhbd3HzvrNNhoSaBgOb4sSsseULL5NFBQNcT+0sRfjsgWzF5"
        "hKExghKwd74j8l5ke3BqKd1UgM3Geb1VC74FXuBVLOY5RNbqtqD3BncJgB89sqU6b"
        "CtOf6kStVSiplrE5eqa2eWHPKyCTc9Y4SKyi8PjlVUqc/NMEm7BTQnvy65+7REafI"
        "veeDF9kIORttPXK3UJ/uNtBL3LWra1Mmtt0NrCM1/lD1/IvynTxAlsMfoLCgAICuM"
        "Sr6DHEzLis5Tesi9/1iAdcpebDVMDD2O5aiPWCKNcShsry2k5btGf0mED1km2CSis"
        "/SwTVqpzghuKzIo9s5ihCfH/VTkMA8zGxDDAHJfDiSe3VsTPEtQQ6klqpQAdfYjPk"
        "7ZB43vX0VG9pA2seO2CPvEpw+qxO5F8Rg12TzILFT1ovktwh2Ss1l2DmgOPhJV4IJ"
        "X1//N3tYpAQ01PnDqRXPiz0H7m3FsmYGpz6FqKdigc5js3iy9ppd04kG2tok3Rst"
        "bfJbiW+ZT+snJP5fZhonA1HMfsb0r/1lPxHwoQC6JGcS8ygyhM6Wao6Olq/BjcMZD"
        "gQSS9zQo9zOFyy/jCThSRt32/YqgnufK+kbagt9aSFYkx5hMgKwXSYGApQMEZ7ru"
        "P5zVsYRyHszKYnTvYyD0kDtYoqjTupzDW6h2MN61XeGq2folpnzo/O7Nep0squ7A7"
        "Cr7KHB4mvmqOztaJUoFlwMIWqL/4ulxs2rJBB35GDwWASLSCnYwB9mQ3+tYu0Bsu4"
        "mQN1CiFonwlzjN/M+cRkVJR7YRe7jFxr8q6NYtjWz1rVmkS6ZWl0095sgy7fVh02D"
        "MxnaaXxE7lp9goTRxmOFs7M4cBStN5uSBRmA2h0SxeCRotyTQ32CfHeCgUwUZGer5"
        "inSyDg3S3+bImLqAYfrw1jqrlBTG2aQqPZcuAlNZJnQTT/GQtmjC6uRgS/1gqYIxM"
        "R1QBI8x72C7fO9OTDbphW6vNOJIOtBXvAdcyF4wKZOdfCgfjzEnuKpfpRAX3zUr6"
        "2R8LvctFF4eiDQgdqqKdiC4Qf+KKPoKE3x5qXF5BUiSufEkNKC1E9IiMWDUNodnqG"
        "mflnoo4R7D8AHGpilx2Cwe2P5MiNG/ZCDf1WlSpWip4E7fG0wJXCL0vfgVK7APBve"
        "Hqw15zq+BTwg+S9NqzcjC4zuNzWFwA5orn7CeSIZwskKR58F4jHShpwCIll7V7PQ/"
        "blpqUndMwBMsrK3vdOjn7Q0awAsIwOkQRcBGyemnz8krOxbr4s8FCt3ZCOuK4nPWR"
        "E9ANOUJiAU9C71kaQF5gwIWQD6RqKTLMKymdTjFuSVWyuwQovLZ3lPt7fCEoF+ww"
        "Bra++o2A54ML3U+UYU1TIg8kufB14kMftPJXBL80eCfpy5aCNvUyaSAnW8kx/rcYN"
        "2wBMAgESUr0c4xbJG28pn5FStjRlS1sIMvhI8z1ihIovXQCcjTA29gUZRntiFpDD6"
        "JP74T5kjZelSOgRePdXcQoEXqu0PwL4jlnHMbqt+i3Zg0OiBnwhQfMlQhP1Imhez"
        "Ks8rhj7rJpRdwH5mI05Fexen0u3nIhDUyV5PTPCEle/87YZ1DNm94VYeaEwheeNqv"
        "LPaFgoBczl3nlO6xw8W4qXrYt+mECZAotgQ93Ye3gie3EwsxMoGDRpOYCWCEWj4dz"
        "7mKeXEWBXUS7pjAzIScb+9EC8fQdbvAQIWHG4llv/z6wjpDKxQOhr6hP0xhphJ3wo"
        "kol/Tg5nItswC5uM/ztBcT3zywTLYFD4RoUe9eHsGvXs/yCcMG+WwXhy7D1IT+Qb"
        "sUuSkrgWZeS1nHoyoifClLCfFrxuUhlJFbRCsraFJ6cbE3GRal+dFD7GWKfmiv8bp"
        "sg2q/vIzUpl8PoUu5bDLdGSWoPvW3EvTff9DjrIfw9TwyUOQLnCthpxWeMU54k6p"
        "T5Emx46LKZO9Vf73bccgnIx3lCr/ZcFAE7fHXvK9N0SGHlZw7mzl07Hxfg5QLSDx"
        "rNoXBBJpM3SfMLVMzeZ5R1Rpy4NZoLxUbJGU0RiPKKIo2f/3/qIbAxrY2P5CMP6R"
        "Fe2UyzRe+4z4cXCDcrbXYP4IxrVbAhTUG3+C+/B9RocUeoHt2jnlOHFtx5jfqAXM1"
        "osiCMrytMfjd2UdGJ/vCYjYBdz6Hys9YQ3E17FRwwZGvrZF8G4+UuUA7nnDZZN3P"
        "ITviuPWuRwhXOqGc5A7ce1hbkApaVlo01OQtRU/lsg64t/TxHE5/IYXyrngbhcyoC"
        "lDElpgc62+eayD3Im+i7y2E+vUjCz1T/Le+Sh9zBBd9NUU/09JhWeIQA4eXGDX9k"
        "dZBfdbtK6NkFdNkSme8QyGzR0K5VqA65BD/nzrzQvCXdu/Ulopwc863+yRHBb0qBh"
        "JXAqHMlfLAV/ViOjCN/LAl4fdbTp8vg3p7fqu+lpyNvOfTZcJKrk0LiG+N4ttMPD"
        "EkVBdKbJjLUQRJGFSPnDhO3cKza5zAkqIYcDIegCq/MCW0ULo8Rd4v3loKg72aiQ"
        "uGpW+OUmunPkXsBMlJjXWDjZ3gmO63Nq9RXIICF7n7rd/GQLTb86I2qt1W4T87dc"
        "aPutfmUX52KQQ0VUhQQvQAp4IRvPsodeFKJG7idO4bj40O+iKMTJbGuYPm03XDEp"
        "kTTJJMWF9quL5vRp1TvCNmiQQw9irmjY5pdSIKFI3txU6YlCiq4cqXKmqyMjQpAb6"
        "ik4AJFuQ1ipl/3Ih/aLONdzFfa+o51KebLCOw3hNI9J6BAkosr4Dfg35L/COKerr9"
        "1CgliQPXDh7egj5s9FASDQe5kBLPP4NqXn0IGqgG/yYdfc1i0EDR+ln6cymt3X0u"
        "T+Kd5SazPg9UjEmwdOaK9pCItOp5/w9A+an/FhUcO+Wak4AXlgY82ts8Omcg3ARJ"
        "dwle+0Z7xshHH7dTwI/peXkpj65JZc6KUkyacpTeB6dMhbDVi/kQrdpRlYmoCRRh"
        "H9DYlS4TzMfBcjfg+SjJVtFlMn+gXna1eeFTmAKWics87tugTJ7EI3sRZ6NImQZ+h"
        "51eINfSaVrnQQbYVP8aSECpSVQjkyezMzwtHC/gToUem4q1ZeYCCxLqGiKtMclD1H"
        "XjKZv4UtURWYh4OIQaxMyXUlIVWkpEBmLo4Vbs42efEXv2UGNJ/0WbT5p/thcu/Ne"
        "rBxd2ngtDn5nhhIDg/52psjPOWBG6fxAMAcRGQxtE4LuahftPHuAt9CkeUWWOM05B"
        "iNOpFJMQxpLqQlVIuN3/VFNEwzHGcvR2Q938FljJ1u3QamGCmrHyVHTMRE1SeWjtd"
        "vCItk98Gxs49y4wnAETtbfHqdS1ZpOjqEg8+MzrJa70a69/16//gDcQ8EXBwzk6U7"
        "TBdD+Q251zPDJ7NYeZ3sLiraXn2Pt3bF67W5kFdOFYYmHSWcnkzQ05UClk5HvWwEK"
        "EFg8S/hNu6in0jKHy0Q1RzVrF5c/C18RiRgvrOainDVuR0wttwpjdJVoRW18+3nx"
        "jMdfV4eJLd8Z477NMrov62YJiKdKXEKkJg2C/yz6baQpwX403RjroJMXwVorGgp6G"
        "N/uXoMWQockCl1kFGfIHTWpEpcEcqeyQs8rEdnQ/wuR+LJYoBlIScj9L5SPA02ItV"
        "W7eKUWGKuzrZIjVp17URzgcFw46u1Ap8FO4drpeOgvfb1SGchdGPcFu6xTxtKddYd"
        "0ycbnOQRAvD0seX7sBuWL/XNT3N+RuFbU+OcProntQeJpgKXzzTElIh0f4vKXyYFg"
        "Xz68eWht9Dv/ilDPFLdWD7g4zDXdPmRSLSfDW8hbKBKHu4cTQpw7UxdIanNIHBFYe"
        "Ra4qMzvGC0NELF0ikczUAhq0JvOU309M9ELIGSmrnvorDvCW238lOrFe7XviUn9J"
        "xJ77EmIPI2AgMVRgvcJgrQAavUcKoqO1yNH9OVbIItFtvJkuP4dfrMXjaPb/jNfh6"
        "Jf1OsiauwkKhZ8zRm+QLEkOawXHXXkc1Oe+RIaGQJPUl9vNptPDnemUGSf0wrhKYW"
        "6veKlcbDCHBNN8wMQVQpQVZDd1Ok73XLWvhvou8nWDCXR5eVu3bod02ImaQIeXCi9"
        "3IQ90jjkNl/4B1ktsk98bZDr+S1+WhtaaOqD8OrxB3Dh3wqs8W+EFaWSa3u0B1Zv"
        "i2H2q7uDrGQFIaMrLu3al3BOlUrUBMEDvkpYgGsq/fKw8zR3P3DpbSz1Byz6pbLm"
        "cZuwSd9lHMKB4aQXOVJ8uVF8S4nPvOp2LoBAhIKL2qxUcqS4BBc0SYK8cf9OwbKg"
        "qpnEcm6guOCsmXtnAwkef7c118ok4VV19Q4wQIV3ndFggEBwzeibZKDc+Klf9dEjD"
        "HtYIhaRmwCUApUt3eSL8anb51LngdsqqJqVksqD4Lm+Z7Z1jSbYLxLpyj9WNrGUg"
        "pYnFMWdqtNnJPyprGqoKuK3AzvoR2D60qzd3wYypl4XSyRik5o/NdNZyqmdBAUKZr"
        "/XMsfvN8cTMXOZ9wTd5YLVaJM2ADFm8YVPaPjLIucplbhe13D87PUkL2hYZaSWsdp"
        "uyN/P+wEkjjWt4avvpbtvFF7MMAZ5pZ88oR4uAzkk99z5NaNk4zeGdXCrnUuB+MyQ"
        "DseeFQmfTJ0b1+V90xXNDlRX/UpwDZ2BxpRL2hTc8LxhMHzzKmMJXNm3ZinKq2RP"
        "IpChdGICnPXkD0qOi8a4kgRbuc6U5XKYJq3W9vw2tGpyfkExv5WcOfO6kNP1fj/le"
        "ha6E7zLiJlfUijaiF5c2xxUSadZ6N+UQ9yTrBJxbbABfCkUb4aDjvEyhkNKuhAFvk"
        "OMP6DUPdChHM8Grwv/Lpyc1C+/mRp3bBKv7WM0w3q+gApIx8fFA76y9aM5lqVjuS"
        "Pc2QRfFcmpnRKDtP0glfpqyLUCtzfQDcCaE2zJ7P5DjR0HZVgFWMbXYJDA5tieoc"
        "P5++uHherKDutpCaEhHNtv58DygL+7WQL63or9r7ijpXfQKDMv9xfjzva0dkQukk"
        "YbYWHf/hmJmW2JpYtFVdc7kCFl8UTs79pJcKVKJAnTkiYD9sSfQA/azUSWNFNt/S"
        "CCba8AlUZhaFZ9kc8BxMEpJC7I2m3zEmJusHYi7GaQ0kgLpPiCsK3Z3L5srFJ5X6"
        "zG6c2RZRlrJmx9UdSbo6NBsc8N8Z09QeZr9ThDS9GNrhIG00hCPNa/q5J7H5/BZ3"
        "e3E0LGpopMPGnpyElu+7H8DPlWwIPglIf+rTCciVB1YRHmk/egWVxYPH+CpCMijv"
        "S2A8g+PxaCpNa0UH5oLsBk2yUz9gTl9iZo5g49r5eAX74aEsRDHO7J1cmCmu00no"
        "ZOCMUyt/P0MvXN6otr02rWlmV+WUjjFh2HLl5doU3bmWpt0Nd+I9+K2qOOhjWJxf"
        "r5H/pQkbpDFW+PxDwYd6+AnEu7hmjJzhardAJ4KhrUYOGVo7epKUy1Lhtd1G/yUBX"
        "M3+WYBflWytReM4pcnih/XubUDmqeVM/qpwIBIOXaENzG6ESN1gaiYpXR5bai023"
        "y0cRgAPcxZSWKOPRZNtJaR7vbuVUVrj5WGCpqsR8gQFObsVa97exOx2yTn076pQr"
        "gj1AnYGVeCkIc8/Xh/pT44xS9WQJPeagr4ocBSGH12j/Arib3SntjqXqPUwckYP8s"
        "j+5RdBu+BDWF5gUFhxz40GNdrcTUtJSBDuOyMVMWlz8h4c7CW/I7aDePE2J9jzho"
        "TuWSbLUEKm//boNpOeh1+8eR2n11ltzwb0XBbTfQxnkXrr/FSIiZg737uGpHue20m"
        "wqXe/Juk8WQXa52ejq8E9Ig3QuDYGV0vnUm4dTN/XHZtXuGc2T1xeqCn1WiLOcqZ"
        "RUvr94QByTWdOTPmbZgbavNBlTHLhvaZzHme6x68CUIHwg8v15q7StCwY+foQSux"
        "UzUa36Me+KZekbU0lVz9im+27YEVf/haNc/TewLELFrbyTlRYSqPiZDt61wmttKOe"
        "9sFHgErXViIdjXcTXd6iwgYmHnY6uhjxvFax505+urlmhcD+XSPoEW2T6WPRuSNvD"
        "tKdW530GxFo5IX90RjJ/YcoOHSxSbeqofxSBfyvujLlMP0Tz26u6WG6kZHUDJ1Ma"
        "gU04WECNDC02lF4Jq2fvyUegzD4XecuyNWqw7oBN4xqcKIBMP1qqVoYibzL3k7hHR"
        "CD4zMsHd0EbtEGnOXQqJl43UbZM1MjHdp2T7XguqoEIm43sSATyLBOe6ICO3FXoKh"
        "rkaXQ0ojR7eOHjm44UaDKWfvaefcfD+IpbAw7wUkJwyT17mjMV7RWl4VUk8XKzWM"
        "nrrzWLXLedlGvEkXs/imR2Ukw8yqHif1veTzkeKbjCVb/zN7+iaKEnrxSQ4RzX0Y"
        "YaOjH0GQjJt6VY8OKsql55z+7cm3pZysUCYemiFInblVlPbL4ipEyNrgwNi+8PqE5"
        "nETItL65ZAkQsE4Q1o+IEeJtAUl8WXtVkqcxdRCH74DXC4Cg8A7gGUaT3G1hlAoz"
        "PzekEW3stPvH6EyCWCTomb0BW9humsEqDt3DlcXOUMZV1byF4OeaIa4EVCD2Xr0K"
        "Q36qTCwqCxyZo/jxrco7/gX9SCqZXmzZDXpd5rMDeoGuX81CpClQhxiJd6x2pKC9"
        "+IdCRf8OSglbpX3ETqelowu0+d1znGu6+KYW/PoGJ5ZY3IkQg19KUsZzEtZhxrGe"
        "D/KXh8XbsAf/je3xZg9rNM/WroJn/ZqbY2hWKx8LFMyZfEVdgwC0JobHIuuKrRxp"
        "R1dPKIC104ukfzp3pKM0WFidTKM+Ah2Nk1K9v5Ap7zSZKMA7MXhmnge1sanPLxeZN"
        "uLFrG4HsKaOauOY38iGNXB/oELgvoRYJxAHTsiZHQT2LbHXFYOfdYeiur/NGeht+"
        "m3JQnHp2vhxBfQxhOxeQS57zkiRdVcgdPCMLh12rDvdnwOYFXFSOoBnZx9zkIHrvt"
        "y2Q5/ev5xbPUX955/jpY0+4YZFpw9btlZB3AMaR1sQfArzMVzfmDwQI/J0Zvvm95r"
        "mOck0AFyJcEEa6VM7/Opie80npHex+74zYu64DpPJBOsoBk5zLFbkEzbY4FHZ7ct"
        "pfLPASoXspOmW8TzjOkSUxEfi5kQkT479dvaY6i275lvpmUUZVLS7gOUHdDQvigh"
        "yx0KUbY1uTfrL0wOv4dmI9Cm30xYYaY/nNwX0MA14Q1Rru8EN3prYeHLcwaHamIR"
        "IoV7B3nKBuAuF7p0uF9MwDpyu="
    ),
}


class DouyinAPIError(Exception):
    """API 调用失败（风控 / 未登录 / 数据缺失）。"""


class RiskControlError(DouyinAPIError):
    """风控 / 登录失效 —— 刷新 Cookie 后重试可能恢复。

    涵盖：403 / 429 / 空响应（风控拦截）、「需要登录后才能查看」等。
    与普通 DouyinAPIError 区分开，便于上层自动重登重试。

    :param permanent: True 表示**确定性拒绝**（如 Argus 门禁），
        重试 / 重登 / 等待都无效。上层据此跳过「开浏览器刷新 Cookie」
        这类无效且会加重风控的动作。
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# ---------------------------------------------------------------- Argus 门禁

# 抖音 2026-08 起上线的 ArgusSecurityPlugin 门禁。命中时 HTTP 403，
# body 形如 "Blocked by ArgusSecurityPlugin Uifid Not Found" /
# "... Signature Not Found"。
#
# 关键区别：这类 403 是**确定性**的（请求形状不符），不是频率风控。
# 重试 / 等退避 / 重新登录都无法通过，反而会加速触发验证码。
ARGUS_REJECTION_MARKER = "ArgusSecurityPlugin"


def _argus_marker(body: str) -> str:
    """从 403 body 中提取 Argus 的具体拒绝原因，便于日志定位。"""
    text = str(body or "")
    if ARGUS_REJECTION_MARKER not in text:
        return "ArgusSecurityPlugin"
    idx = text.find(ARGUS_REJECTION_MARKER) + len(ARGUS_REJECTION_MARKER)
    return text[idx:idx + 40].strip() or ARGUS_REJECTION_MARKER


def _is_argus_rejection(status: int, body: str) -> bool:
    """是否为 Argus 门禁的确定性拒绝（重试无意义）。"""
    return status == 403 and ARGUS_REJECTION_MARKER in str(body or "")


# ---------------------------------------------------------------- msToken

# msToken 缓存：避免每个作品解析都多打一次 mssdk 接口
_ms_token_cache: tuple = ("", 0.0)
_MS_TOKEN_TTL = 1800.0      # 30 分钟

# ---------------------------------------------------------------- msToken 配置
#
# mssdk 生成 msToken 需要一份含 magic / strData 的配置，该配置随抖音更新而
# 失效。内置快照（_MSSDK_CONF）实测已过期（返回 resultCode: -6），会导致
# 真实 token 永远生成失败、静默退化为随机占位。
#
# 参考 jiji262/douyin-downloader 的做法：优先远程拉取 F2 的 conf.yaml
# （该文件由社区持续更新），失败则回退内置快照/上次成功值。
_F2_CONF_URL = (
    "https://raw.githubusercontent.com/Johnserf-Seed/f2/main/f2/conf/conf.yaml")
_MSSDK_CONF_REQUIRED = ("url", "magic", "version", "dataType", "ulr", "strData")
# 配置缓存：成功时缓存 1 小时；失败后退避 5 分钟，避免每次解析都白等超时
_ms_conf_cache: tuple = ({}, 0.0)
_ms_conf_retry_after: float = 0.0
_MS_CONF_TTL = 3600.0
_MS_CONF_FAIL_BACKOFF = 300.0
_ms_conf_lock = threading.Lock()


def _parse_f2_conf(raw: str) -> Dict[str, Any]:
    """从 F2 conf.yaml 文本中提取 msToken 配置段。

    只做最小的 YAML 提取（不引入 PyYAML 依赖）：定位
    ``douyin:`` 下的 ``msToken:`` 块，逐行取 ``key: value``。
    """
    try:
        import yaml  # 可选依赖：装了就精确解析
        data = yaml.safe_load(raw) or {}
        conf = ((data.get("f2") or {}).get("douyin") or {}).get("msToken") or {}
        if isinstance(conf, dict):
            return conf
    except Exception:
        pass

    # 无 PyYAML 时的兜底：按缩进抓 msToken 块
    lines = str(raw or "").splitlines()
    start = None
    base_indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("msToken:") and not stripped.endswith("{}"):
            start = index
            base_indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return {}

    conf: Dict[str, Any] = {}
    for line in lines[start + 1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        if ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if value:
            conf[key] = value
    # 数值字段转回 int（YAML 里是裸数字，兜底路径拿到的是字符串）
    for key in ("magic", "version", "dataType", "ulr"):
        if key in conf:
            try:
                conf[key] = int(conf[key])
            except (TypeError, ValueError):
                conf.pop(key, None)
    return conf


def _fetch_remote_ms_conf(timeout: float = 5.0) -> Dict[str, Any]:
    """远程拉取 F2 配置；失败返回 {}。"""
    try:
        resp = requests.get(_F2_CONF_URL, timeout=timeout, proxies=_proxies())
        if resp.status_code != 200:
            return {}
        conf = _parse_f2_conf(resp.text)
    except requests.RequestException:
        return {}
    if not isinstance(conf, dict):
        return {}
    if any(k not in conf for k in _MSSDK_CONF_REQUIRED):
        return {}
    return conf


def _load_ms_token_conf(timeout: float = 5.0) -> Dict[str, Any]:
    """返回可用的 msToken 生成配置：远程（缓存）→ 内置快照。"""
    global _ms_conf_cache, _ms_conf_retry_after
    now = time.time()
    with _ms_conf_lock:
        cached, cached_at = _ms_conf_cache
        if cached and (now - cached_at) < _MS_CONF_TTL:
            return cached
        if now < _ms_conf_retry_after:
            return cached or _MSSDK_CONF

    remote = _fetch_remote_ms_conf(timeout=timeout)
    with _ms_conf_lock:
        if remote:
            _ms_conf_cache = (remote, time.time())
            _ms_conf_retry_after = 0.0
            return remote
        # 失败：记住退避窗口，期间直接用（可能过期的）内置快照
        _ms_conf_retry_after = time.time() + _MS_CONF_FAIL_BACKOFF
        stale, _ = _ms_conf_cache
        return stale or _MSSDK_CONF


def _gen_fake_ms_token() -> str:
    """随机占位 msToken（长度 184，与真实 token 一致）。"""
    return "".join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(182)) + "=="


def _is_valid_ms_token(token: str) -> bool:
    return len(token.strip()) in (164, 184)


def _gen_real_ms_token(timeout: float = 8.0) -> str:
    """调用 mssdk 接口生成真实 msToken；失败返回空串。

    配置优先用远程 F2 conf（见 :func:`_load_ms_token_conf`）：内置快照里的
    ``strData`` / ``magic`` 会随抖音更新而失效（实测内置值返回
    ``resultCode: -6``，导致真实 token 永远拿不到、静默退化为随机占位）。
    """
    conf = _load_ms_token_conf(timeout=timeout)
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
    """返回可用的 msToken：Cookie 中的 → 缓存 → mssdk 生成 → 随机占位。

    mssdk 生成结果会被缓存（默认 30 分钟）。否则每个作品解析都会多打一次
    mssdk 接口，请求量翻倍且更容易触发风控。
    """
    global _ms_token_cache
    for item in cookie.split(";"):
        item = item.strip()
        if item.startswith("msToken="):
            token = item.split("=", 1)[1].strip()
            if _is_valid_ms_token(token):
                return token

    cached, cached_at = _ms_token_cache
    if cached and (time.time() - cached_at) < _MS_TOKEN_TTL:
        return cached

    real = _gen_real_ms_token()
    if real:
        _ms_token_cache = (real, time.time())
        return real
    # mssdk 不可用：用随机占位，并缓存以免每次重复请求
    fake = _gen_fake_ms_token()
    _ms_token_cache = (fake, time.time())
    return fake


# ---------------------------------------------------------------- 参数与签名


def _extract_uifid(cookie: str) -> str:
    """返回 query 用的 uifid 值；默认**留空**（现状行为，实测最优）。

    背景：Argus 会以 ``Blocked by ArgusSecurityPlugin Uifid Not Found``
    拒绝请求，看名字像是「缺 uifid」，但实测证明**补上反而更差**：

    ============= =========== ==========================
    uifid 传值     成功/总数    失败时的报错
    ============= =========== ==========================
    空（现状）      3/4        ``Uifid Not Found``
    填 Cookie 值    1/4        ``Signature Not Found``
    ============= =========== ==========================

    Cookie 里的 ``UIFID`` 是 320 字符的加密值，直接塞进 query 会让门禁
    从「缺 uifid」推进到「签名不匹配」——即 uifid 只是第一道检查，真正
    决定放行的是**页面上下文签名**（``x-secsdk-web-signature``），而它由
    页面 JS 运行时生成，纯 HTTP 客户端无法伪造。

    参考项目 jiji262/douyin-downloader 的结论与此一致：它最终放弃在 CLI
    路径解决，改为在 desktop 版走 Electron 隐藏窗口（``page_bridge``）
    用页面 SDK 补 uifid / timestamp / x-secsdk-web-signature。

    因此这里**刻意保持留空**，并在 fetch_video_detail 里对
    ``Signature``/``Uifid`` 类 403 直接判定为「不可重试」，避免无效重试
    反而加速触发风控。若将来拿到正确的 uifid 形态，改此处即可。
    """
    return ""


def default_query(cookie: str = "") -> Dict[str, str]:
    """构造与浏览器一致的请求参数（风控校验的重要部分）。

    屏幕/硬件参数取自**当前固定的浏览器指纹**，而不是写死值 ——
    否则 query 与 a_bogus 签名内的指纹互相矛盾（实测签名随机出
    1881x876、query 恒为 1536x864），与真实浏览器自洽性相悖。
    """
    _ensure_profile()
    geom = _FP_GEOMETRY
    # 指纹不可用（无 gmssl）时退回写死值，保证参数完整
    screen_w = str(geom.get("screen_width") or 1536)
    screen_h = str(geom.get("screen_height") or 864)
    platform = _fp_platform(_UA)
    query = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows" if platform == "Win32" else "Mac",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": screen_w,
        "screen_height": screen_h,
        "browser_language": "zh-CN",
        "browser_platform": platform,
        "browser_name": "Chrome",
        "browser_version": "139.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "139.0.0.0",
        "os_name": "Windows" if platform == "Win32" else "Mac",
        "os_version": "10" if platform == "Win32" else "10.15.7",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "support_h265": "1",
        "support_dash": "1",
        "uifid": _extract_uifid(cookie),
        "msToken": ensure_ms_token(cookie),
    }
    return query


def sign_url(url: str, user_agent: str = None) -> tuple:
    """给 URL 追加签名（a_bogus 优先，X-Bogus 兜底）。

    指纹与 UA 均取自**全局固定的 profile**，保证：
      * 签名内嵌的 UA == 返回的 UA == 请求头 UA
      * 签名指纹 == default_query 屏幕参数的来源
    :return: (签名后的完整 URL, 请求应使用的 User-Agent)
    """
    _ensure_profile()
    ua = user_agent or _UA
    base, sep, query = url.partition("?")
    if ABogus is not None:
        try:
            signer = ABogus(fp=_BROWSER_FP, user_agent=ua)
            params_with_ab, _ab, ab_ua, _body = signer.generate_abogus(query, "")
            return f"{base}?{params_with_ab}", ab_ua
        except Exception:
            pass  # 回退 X-Bogus
    signed, _, _ = XBogus(ua).build(url)
    return signed, ua


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
        throttle()
        resp = requests.post(
            _TTWID_REGISTER_URL,
            data=_TTWID_BODY,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10,
            proxies=_proxies(),
        )
        ttwid = resp.cookies.get("ttwid") or ""
        if ttwid:
            _ttwid_cache = ttwid
            return ttwid
    except requests.RequestException:
        pass
    return ""


# ---------------------------------------------------------------- 代理

# 代理配置（用于规避 IP 维度的风控；空表示直连）
_proxy_url = ""


def configure_proxy(proxy: str = "") -> None:
    """设置 HTTP/HTTPS 代理（供上层从 config.json 设置）。空字符串 = 直连。"""
    global _proxy_url
    _proxy_url = str(proxy or "").strip()


def _proxies() -> Optional[Dict[str, str]]:
    """返回 requests 用的 proxies 参数；未配置代理时返回 None（直连）。"""
    if not _proxy_url:
        return None
    return {"http": _proxy_url, "https": _proxy_url}


# ---------------------------------------------------------------- 请求节流

# 全局最小请求间隔（秒）：所有对抖音的请求共享，避免突发流量触发频率风控。
# 参考 jiji262/douyin-downloader 的实践（默认约 2 req/s，且建议降并发）。
_rate_lock = threading.Lock()
_last_request_at = 0.0
_min_interval = 1.0        # 基础最小间隔（秒）
_jitter_ratio = 0.5        # 额外随机抖动：0~50% 的基础间隔


def configure_rate_limit(min_interval: float = None,
                         jitter_ratio: float = None) -> None:
    """配置请求节流参数（供上层从 config.json 设置）。"""
    global _min_interval, _jitter_ratio
    if min_interval is not None:
        _min_interval = max(0.0, float(min_interval))
    if jitter_ratio is not None:
        _jitter_ratio = max(0.0, float(jitter_ratio))


def throttle() -> float:
    """阻塞直到满足最小请求间隔，返回实际等待秒数。

    间隔 = min_interval + 随机抖动，使请求节奏不像脚本。
    """
    global _last_request_at
    with _rate_lock:
        now = time.time()
        target = _min_interval
        if _jitter_ratio:
            target += random.uniform(0, _min_interval * _jitter_ratio)
        wait = _last_request_at + target - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()
        return max(0.0, wait)


# ---------------------------------------------------------------- 详情接口


def fetch_video_detail(
    item_id: str,
    cookie: str = "",
    timeout: float = 15.0,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """调用 /aweme/v1/web/aweme/detail/ 获取作品详情。

    依次尝试 aid=6383 / 1128；403/429/空响应视为风控，指数退避重试。
    每次真实请求前会经过全局节流（throttle()），降低频率风控概率。
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
            throttle()
            try:
                resp = requests.get(signed_url, headers=headers, timeout=timeout,
                                    proxies=_proxies())
            except requests.RequestException as e:
                raise DouyinAPIError(f"详情请求失败：{e}")

            if resp.status_code in _RISK_STATUSES or not resp.text:
                # Argus 门禁是**确定性**拒绝（请求形状不符），重试 / 等待 /
                # 重登都无效，只会加速触发验证码。实测 403 body：
                #   "Blocked by ArgusSecurityPlugin Uifid Not Found"
                #   "Blocked by ArgusSecurityPlugin Signature Not Found"
                if _is_argus_rejection(resp.status_code, resp.text):
                    raise RiskControlError(
                        f"接口风控（{_argus_marker(resp.text)}）—— "
                        "请求形状被确定性拒绝，重试 / 重登均无效",
                        permanent=True)
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
