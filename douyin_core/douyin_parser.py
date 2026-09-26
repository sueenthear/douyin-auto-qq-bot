# -*- coding: utf-8 -*-
"""
douyin_parser.py — 抖音作品解析模块（视频 + 图文图集）
功能：
  1. 从分享文案中自动提取抖音链接（短链 / 网页链接）
  2. 短链接跟随重定向，解析出作品 item_id
  3. 获取作品信息（标题、作者、时长、分辨率、统计数据）
  4. 视频作品：生成无水印播放地址
  5. 图文（图集）作品：解析 images 数组，生成无水印图片地址列表

说明：抖音接口与页面结构会不定期变动。历史上本模块采用多级回退，但
     2026-09 实测后两级（iesdouyin iteminfo、详情页 HTML）均已彻底失效，
     现已摘除，只保留官方 Web API 一条路径 —— 见 ``_fetch_info`` 的说明。
     失败时抛出带提示的异常。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from . import douyin_api

# ---------------------------------------------------------------- 常量

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 分享短链：https://v.douyin.com/vo_5dUe23Oc/
SHARE_URL_RE = re.compile(r"https?://v\.douyin\.com/[\w\-]+/?", re.IGNORECASE)
# 网页版视频/图文链接
WEB_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:douyin\.com|iesdouyin\.com)/"
    r"(?:share/)?(?:video|note|discover)/?[\w\-]*/?",
    re.IGNORECASE,
)
# 任意抖音域名链接（兜底）
ANY_DOUYIN_URL_RE = re.compile(
    r"https?://[\w\-]*\.?(?:douyin\.com|iesdouyin\.com)/\S*", re.IGNORECASE
)

_ITEM_ID_RE = re.compile(r"(?:video|note|share/video|share/note)/(\d+)")
_MODAL_ID_RE = re.compile(r"modal_id=(\d+)")
_ITEM_ID_RAW_RE = re.compile(r"(\d{15,21})")


class ParseError(Exception):
    """解析失败，message 面向用户可直接展示。"""


# ---------------------------------------------------------------- 数据模型


@dataclass
class ImageItem:
    """图集作品中的单张图片。"""
    url: str = ""                                  # 无水印地址（首选）
    fallbacks: list = field(default_factory=list)   # 备用地址（下载失败时依次尝试）
    width: int = 0
    height: int = 0
    ext: str = "jpeg"                               # 文件扩展名

    @property
    def resolution_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "未知"


@dataclass
class VideoInfo:
    item_id: str
    title: str = ""
    author: str = ""
    create_time: str = ""
    duration_sec: int = 0
    width: int = 0
    height: int = 0
    play_url: str = ""            # 无水印播放地址（首选）
    play_url_fallbacks: list = field(default_factory=list)  # 备用地址（下载失败时依次尝试）
    cover_url: str = ""
    digg_count: int = 0           # 点赞
    comment_count: int = 0        # 评论
    share_count: int = 0          # 分享
    collect_count: int = 0        # 收藏
    raw: dict = field(default_factory=dict)  # 原始 JSON，便于排查
    # 图文（图集）作品：内容本体是 images，video 字段里只有 BGM 音频
    images: list = field(default_factory=list)   # list[ImageItem]
    media_type: str = "video"     # "video" | "image"

    @property
    def is_image_post(self) -> bool:
        """是否为图文（图集）作品。"""
        return self.media_type == "image" or bool(self.images)

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def duration_text(self) -> str:
        if not self.duration_sec:
            return "未知"
        m, s = divmod(self.duration_sec, 60)
        return f"{m:02d}:{s:02d}"

    @property
    def resolution_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "未知"


# ---------------------------------------------------------------- 工具函数


def extract_share_url(text: str) -> Optional[str]:
    """从任意文本（如抖音分享文案）中提取第一个抖音链接。"""
    if not text:
        return None
    for pattern in (SHARE_URL_RE, WEB_URL_RE, ANY_DOUYIN_URL_RE):
        m = pattern.search(text)
        if m:
            url = m.group(0).rstrip("，,。；;！!？?）)】]")
            # 有的链接末尾跟着 o@d.Nj 之类字符，只保留合法 URL 部分
            return url
    return None


def _find_item_id_in_url(url: str) -> Optional[str]:
    m = _ITEM_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _MODAL_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _ITEM_ID_RAW_RE.search(url)
    if m:
        return m.group(1)
    return None


def _format_create_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return str(ts or "")


def _walk_find_video_dict(obj) -> Optional[dict]:
    """递归查找形如 {desc, video, author, statistics} 的视频信息对象。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("video"), dict) and ("desc" in obj or "author" in obj):
            return obj
        for v in obj.values():
            r = _walk_find_video_dict(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_find_video_dict(v)
            if r:
                return r
    return None


def _clean_url(url: str) -> str:
    """清理 URL 中的转义与尾部杂质。"""
    if not url:
        return ""
    url = url.replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")
    url = re.sub(r"[\\'\"]+$", "", url.strip())
    return url


# ---------------------------------------------------------------- 解析器


class DouyinParser:
    """抖音视频解析器。

    :param cookie: 可选。填入登录后的 Cookie 可显著提高成功率，
                   例如 "ttwid=xxx; msToken=xxx; ..."
    :param timeout: 单个请求超时（秒）
    """

    def __init__(self, cookie: str = "", timeout: float = 15.0):
        self.timeout = timeout
        self.session = requests.Session()
        headers = dict(DEFAULT_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        self.session.headers.update(headers)

    def set_cookie(self, cookie: str) -> None:
        """设置/更新 Cookie（登录后复用登录态）。"""
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)

    # ----- 对外主流程 -----

    def parse_text(self, text: str) -> VideoInfo:
        """输入分享文案/链接，返回解析结果。"""
        url = extract_share_url(text)
        if not url:
            raise ParseError("未在输入内容中找到抖音链接，请粘贴完整的分享文案。")
        return self.parse_url(url)

    def parse_url(self, url: str) -> VideoInfo:
        item_id = self._resolve_item_id(url)
        info = self._fetch_info(item_id)
        return info

    # ----- 链接解析 -----

    def _resolve_item_id(self, url: str) -> str:
        """把任意抖音链接解析为 item_id。"""
        m = _ITEM_ID_RE.search(url)
        if m:
            return m.group(1)

        if "v.douyin.com" in url:
            try:
                resp = self.session.get(
                    url, allow_redirects=True, timeout=self.timeout
                )
                final = resp.url
            except requests.RequestException as e:
                raise ParseError(f"短链接请求失败：{e}")
            item_id = _find_item_id_in_url(final)
            if item_id:
                return item_id
            raise ParseError(f"短链接跳转后未能识别视频 ID（{final[:120]}）")

        # 其他域名：尝试从 URL 直接找数字 ID
        item_id = _find_item_id_in_url(url)
        if item_id:
            return item_id
        raise ParseError(f"无法从链接中识别视频 ID：{url[:120]}")

    # ----- 信息获取（多级回退） -----

    @staticmethod
    def _has_content(info: VideoInfo) -> bool:
        """是否拿到有效内容：视频看播放地址，图文看图片列表。"""
        return bool(info.play_url) or bool(info.images)

    def _fetch_info(self, item_id: str) -> VideoInfo:
        """获取作品信息。

        **只有一条有效路径**：官方 Web API（``/aweme/v1/web/aweme/detail/``）。

        历史上还有两级回退，2026-09 实测均已彻底失效，故摘除：

        * ``_fetch_from_api``（iesdouyin iteminfo）：恒返回
          ``status_code=11110 / encrypt_data_miss``，旧接口已废弃。
        * ``_fetch_from_page``（详情页 HTML）：页面已不再内嵌
          ``aweme_detail`` / ``play_addr``（``_ROUTER_DATA`` 消失，
          ``RENDER_DATA`` 只剩框架数据），解析不到播放地址。

        摘除的原因不只是「没用」：它们会各发一次注定失败的请求，
        白白增加风控暴露面；更糟的是把风控错误降级成误导性的
        「作品可能已删除」。两者仍保留为方法（未来若抖音回退页面结构
        可复用），但不再进入回退链。
        """
        cookie = self.session.headers.get("Cookie", "")
        try:
            detail = douyin_api.fetch_video_detail(item_id, cookie=cookie)
        except douyin_api.RiskControlError:
            # 风控必须向上传播：上层据此决定「刷新 Cookie 后重试」。
            raise
        except (douyin_api.DouyinAPIError, requests.RequestException) as e:
            raise ParseError(
                f"解析失败（官方接口不可用：{e}）。"
                "建议：先登录抖音后重试（python main.py --login）。")

        info = self._build_info(detail, item_id)
        if not self._has_content(info):
            raise ParseError(
                "解析失败（官方接口未返回播放地址或图片）。"
                "该作品可能已删除 / 私密，或需要登录。")
        return info

    def _fetch_from_api(self, item_id: str) -> VideoInfo:
        api = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"
        resp = self.session.get(api, params={"item_ids": item_id},
                                timeout=self.timeout)
        if resp.status_code != 200:
            raise ParseError(f"接口状态码 {resp.status_code}")
        data = resp.json()
        item_list = data.get("item_list") or []
        if not item_list:
            raise ParseError("接口返回为空（可能需要 Cookie）")
        return self._build_info(item_list[0], item_id)

    def _fetch_from_page(self, item_id: str) -> VideoInfo:
        page_url = f"https://www.douyin.com/video/{item_id}"
        resp = self.session.get(page_url, timeout=self.timeout)
        if resp.status_code not in (200, 403):
            raise ParseError(f"详情页状态码 {resp.status_code}")
        html = resp.text

        payload = self._extract_page_json(html)
        if payload:
            obj = _walk_find_video_dict(payload)
            if obj:
                return self._build_info(obj, item_id)

        # 兜底：直接在 HTML 中找 playwm 播放地址
        m = re.search(r'https://www\.douyin\.com/aweme/v1/playwm/\?[^"\'\\\s]+',
                      html)
        if m:
            url = _clean_url(m.group(0))
            info = VideoInfo(item_id=item_id)
            info.play_url = make_watermark_free(url)
            info.title = _guess_title_from_html(html)
            return info

        raise ParseError("详情页中未找到视频数据")

    @staticmethod
    def _extract_page_json(html: str) -> Optional[dict]:
        """从详情页 HTML 中提取内嵌 JSON（_ROUTER_DATA / __pace_f / RENDER_DATA）。"""
        candidates = []

        m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
                      html, re.S)
        if m:
            candidates.append(m.group(1))

        m = re.search(r"self\.__pace_f\.push\(\s*\[[^\]]*\],\s*(\{.*?\})\s*\)\s*</script>",
                      html, re.S)
        if m:
            candidates.append(m.group(1))

        m = re.search(r'<script id="RENDER_DATA" type="application/json">(.*?)</script>',
                      html, re.S)
        if m:
            raw = m.group(1).strip()
            try:
                raw = urllib.parse.unquote(raw)
            except Exception:
                pass
            candidates.append(raw)

        for cand in candidates:
            try:
                obj = json.loads(cand)
                if isinstance(obj, (dict, list)):
                    return obj
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    # ----- 组装信息 -----

    def _build_info(self, item: dict, item_id: str) -> VideoInfo:
        video = item.get("video") or {}
        author = item.get("author") or {}
        stats = item.get("statistics") or {}

        info = VideoInfo(item_id=str(item.get("aweme_id") or item_id))
        info.title = (item.get("desc") or "").strip()
        info.author = (author.get("nickname") or author.get("unique_id") or "").strip()
        info.create_time = _format_create_time(item.get("create_time"))
        info.duration_sec = int((video.get("duration") or 0) / 1000)
        info.width = int(video.get("width") or 0)
        info.height = int(video.get("height") or 0)
        info.digg_count = int(stats.get("digg_count") or 0)
        info.comment_count = int(stats.get("comment_count") or 0)
        info.share_count = int(stats.get("share_count") or 0)
        info.collect_count = int(stats.get("collect_count") or 0)
        info.cover_url = _first_url((video.get("cover") or {}).get("url_list"))
        info.raw = item

        # 图文（图集）作品优先判定：内容本体在 images 数组里。
        # 注意：这类作品的 video.play_addr 指向的是 BGM 音频（audio/mp4），
        # 若沿用视频逻辑，会下载到一个「纯黑无画面」的音频文件。
        images = self._build_images(item.get("images"))
        if images:
            info.media_type = "image"
            info.images = images
            info.width = images[0].width or info.width
            info.height = images[0].height or info.height
            if not info.cover_url:
                info.cover_url = images[0].url
            return info

        # 无水印地址：官方接口的 bit_rate 阶梯取最高画质直连优先，
        # 签名 play 端点兜底；最后回退到 playwm 替换
        candidates = douyin_api.pick_video_candidates(video)
        if candidates:
            info.play_url = candidates[0]
            info.play_url_fallbacks = candidates[1:]
        else:
            best = _first_url((video.get("play_addr") or {}).get("url_list"))
            if best:
                info.play_url = make_watermark_free(_clean_url(best))
        return info

    @staticmethod
    def _build_images(raw_images) -> list:
        """把 detail 的 images 数组转成 ImageItem 列表（优先无水印地址）。

        抖音图集字段含义：
          url_list          → 无水印（模板 tplv-dy-aweme-images）
          download_url_list → 带水印（模板 tplv-dy-water-v2），仅作兜底
        """
        result: list = []
        for raw in raw_images or []:
            if not isinstance(raw, dict):
                continue
            urls = _clean_url_list(raw.get("url_list"))
            if not urls:
                urls = _clean_url_list(raw.get("download_url_list"))
            if not urls:
                continue
            # jpeg/png 通用性优于 webp（稳定排序：同优先级保持原顺序）
            urls.sort(key=lambda u: 1 if _image_ext(u) == "webp" else 0)
            first = urls[0]
            result.append(ImageItem(
                url=first,
                fallbacks=urls[1:],
                width=int(raw.get("width") or 0),
                height=int(raw.get("height") or 0),
                ext=_image_ext(first),
            ))
        return result


# ---------------------------------------------------------------- 去水印


def make_watermark_free(url: str) -> str:
    """把带水印的 playwm 地址转换为无水印地址。"""
    url = _clean_url(url)
    if "playwm" in url:
        url = url.replace("playwm", "play", 1)
    return url


def _first_url(url_list) -> str:
    if not url_list:
        return ""
    for u in url_list:
        if u:
            return _clean_url(u)
    return ""


# 图片扩展名（按此顺序匹配 URL 路径末段）
_IMAGE_EXTS = ("webp", "jpeg", "jpg", "png", "heic", "avif")


def _image_ext(url: str) -> str:
    """从图片 URL 推断文件扩展名；无法识别时按 jpeg 处理。"""
    path = urllib.parse.urlsplit(url).path.lower()
    for ext in _IMAGE_EXTS:
        if path.endswith("." + ext):
            return ext
    return "jpeg"


def _clean_url_list(url_list) -> list:
    """清理 URL 列表：去转义、去空、去重，并保持原有顺序。"""
    out: list = []
    seen = set()
    for u in url_list or []:
        if not isinstance(u, str):
            continue
        cleaned = _clean_url(u)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _guess_title_from_html(html: str) -> str:
    m = re.search(r'<title>([^<]{2,80})</title>', html, re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        return re.sub(r"[_\-|].*$", "", title).strip()
    return ""
