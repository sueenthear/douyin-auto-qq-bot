# -*- coding: utf-8 -*-
"""qq_bot — 抖音分享链接 → QQ 自动解析机器人（NapCat / OneBot 11）。

监听配置里圈定的私聊/群聊（白名单见 allow.txt），收到抖音分享链接后自动：

* 视频：引用回复「正在解析中…」→ 发送视频 info → 发送无水印视频文件
* 图文：引用回复「正在解析中…」→ 发送图文 info → 合并转发发送所有图片
"""

from __future__ import annotations

from .config import (BotConfig, ConfigError, load_allow_file, load_config,
                     parse_allow_entry)
from .handler import DouyinQQBot, format_info, guess_media_type
from .napcat import (NapCatClient, NapCatError, build_node, image_seg,
                     reply_seg, text_seg, to_file_uri)

__version__ = "0.1.0"

__all__ = [
    "BotConfig",
    "ConfigError",
    "load_config",
    "load_allow_file",
    "parse_allow_entry",
    "DouyinQQBot",
    "format_info",
    "guess_media_type",
    "NapCatClient",
    "NapCatError",
    "to_file_uri",
    "build_node",
    "text_seg",
    "image_seg",
    "reply_seg",
]
