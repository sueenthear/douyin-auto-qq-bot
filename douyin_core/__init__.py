# -*- coding: utf-8 -*-
"""
douyin_core — 抖音无水印视频解析内核（纯逻辑，无 UI）

由 tiktok_download/Windows（tkinter 桌面版）蒸馏而来：
剥离全部界面代码（tkinter / 进度条 / 输入记忆），仅保留解析、签名、
下载与登录态管理的实现逻辑。

对外入口：

    from douyin_core import DouyinParser, extract_share_url

    parser = DouyinParser(cookie="sessionid=...")
    info = parser.parse_text("2.35 复制打开抖音... https://v.douyin.com/xxxx/ ...")
    print(info.title, info.play_url)

命令行：

    python -m douyin_core "<分享文案或链接>" [--download 目录] [--json]
"""

from __future__ import annotations

from .douyin_parser import (
    DouyinParser,
    ParseError,
    VideoInfo,
    extract_share_url,
    make_watermark_free,
)
from . import douyin_api, downloader, login_manager
from .douyin_api import (
    DouyinAPIError,
    RiskControlError,
    configure_proxy,
    configure_rate_limit,
    ensure_ms_token,
    ensure_ttwid,
    fetch_video_detail,
    pick_video_candidates,
    sign_url,
    throttle,
)
from .downloader import (
    DownloadError,
    build_filename,
    download_file,
    format_size,
    format_speed,
    safe_filename,
)
from .login_manager import (
    CookieStore,
    LoginError,
    LoginManager,
    LoginState,
    has_session_cookie,
    verify_cookie_online,
)

__version__ = "0.1.0"

__all__ = [
    # 解析
    "DouyinParser",
    "VideoInfo",
    "ParseError",
    "extract_share_url",
    "make_watermark_free",
    # 官方 Web API / 签名
    "douyin_api",
    "DouyinAPIError",
    "RiskControlError",
    "configure_rate_limit",
    "configure_proxy",
    "throttle",
    "fetch_video_detail",
    "pick_video_candidates",
    "sign_url",
    "ensure_ms_token",
    "ensure_ttwid",
    # 下载
    "downloader",
    "download_file",
    "DownloadError",
    "build_filename",
    "safe_filename",
    "format_size",
    "format_speed",
    # 登录态
    "login_manager",
    "LoginManager",
    "LoginState",
    "LoginError",
    "CookieStore",
    "has_session_cookie",
    "verify_cookie_online",
]
