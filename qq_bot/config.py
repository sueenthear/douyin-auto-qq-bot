# -*- coding: utf-8 -*-
"""配置加载：接入信息走 config.json，监听白名单走 allow.txt。

**config.json** —— 只放 NapCat 接入信息与运行参数：

    {
      "napcat": {
        "ws_url": "ws://127.0.0.1:3001/",
        "token": "123456",
        "reconnect_interval": 5,
        "read_timeout": 90
      },
      "download_dir": "downloads",
      "keep_files": false,
      "cookie_refresh_timeout": 60,
      "risk_retry_attempts": 3,
      "risk_retry_interval": 3,
      "request_min_interval": 1,
      "request_jitter_ratio": 0.5,
      "proxy": ""
    }

**allow.txt** —— 监听白名单，一行一个条目，以 ``//`` 开头的整行是注释：

    // 群号用 # 开头，私聊号用 * 开头
    *100000001
    #200000003

**风控重试**：``risk_retry_attempts``（最多尝试解析的次数，含首次，默认 3；
设为 1 表示不重试）与 ``risk_retry_interval``（每次重试前的等待秒数，默认 3）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

GROUP_PREFIX = "#"
PRIVATE_PREFIX = "*"
COMMENT_PREFIX = "//"

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_ALLOW_FILE = "allow.txt"

# 风控重试默认值：最多尝试 3 次（含首次），每次重试前等待 3 秒
DEFAULT_RISK_ATTEMPTS = 3
DEFAULT_RISK_INTERVAL = 3.0

DEFAULT_MESSAGES = {
    "video": "检测到抖音视频分享链接，正在解析中……",
    "image": "检测到抖音图文分享链接，正在解析中……",
    "unknown": "检测到抖音分享链接，正在解析中……",
    "error": "解析失败：{error}",
    "risk": ("解析失败：当前触发抖音风控（{reason}）。\n"
             "已尝试重新拉取 Cookie 仍未成功，请稍后重试。\n"
             "触发链接：{url}"),
}


class ConfigError(Exception):
    """配置错误，message 面向用户可直接展示。"""


def parse_allow_entry(entry: str) -> tuple:
    """把一条白名单条目解析为 (kind, number)。

    :return: ("group"|"private", int)
    :raises ConfigError: 前缀缺失或号码非数字
    """
    if not isinstance(entry, str):
        raise ConfigError(f"白名单条目必须是字符串，收到 {entry!r}")
    text = entry.strip()
    if not text:
        raise ConfigError("白名单存在空条目")
    prefix, body = text[0], text[1:].strip()
    if prefix == GROUP_PREFIX:
        kind = "group"
    elif prefix == PRIVATE_PREFIX:
        kind = "private"
    else:
        raise ConfigError(
            f"条目必须以 {GROUP_PREFIX}（群）或 {PRIVATE_PREFIX}（私聊）开头，"
            f"收到 {entry!r}")
    if not body.isdigit():
        raise ConfigError(f"条目的号码部分必须是数字，收到 {entry!r}")
    return kind, int(body)


def load_allow_file(path: str) -> tuple:
    """读取白名单文件。

    规则：一行一个条目；空行忽略；以 ``//`` 开头的整行视为注释。

    :return: (entries, private_ids, group_ids)
    :raises ConfigError: 文件缺失 / 条目非法 / 白名单为空
    """
    if not os.path.isfile(path):
        raise ConfigError(f"白名单文件不存在：{path}")

    entries: list = []
    private_ids: set = set()
    group_ids: set = set()

    try:
        # utf-8-sig：容忍 Windows 编辑器/PowerShell 写入的 BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError as e:
        raise ConfigError(f"白名单文件读取失败：{e}")

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        try:
            kind, number = parse_allow_entry(line)
        except ConfigError as e:
            raise ConfigError(f"{path} 第 {lineno} 行：{e}")
        if kind == "group":
            group_ids.add(number)
        else:
            private_ids.add(number)
        entries.append(line)

    if not entries:
        raise ConfigError(
            f'白名单为空（{path}），至少需要一行 "*私聊号" 或 "#群号"')
    return entries, private_ids, group_ids


@dataclass
class BotConfig:
    # NapCat 接入
    ws_url: str = "ws://127.0.0.1:3001/"
    token: str = ""
    reconnect_interval: float = 5.0
    read_timeout: float = 90.0
    # 监听白名单（来自 allow.txt）
    listen: list = field(default_factory=list)      # 原始条目（保序，便于展示）
    private_ids: set = field(default_factory=set)
    group_ids: set = field(default_factory=set)
    allow_path: str = ""
    # 下载
    download_dir: str = "downloads"
    keep_files: bool = False
    # 文案
    messages: dict = field(default_factory=lambda: dict(DEFAULT_MESSAGES))
    # 其他
    config_path: str = ""
    process_timeout: float = 300.0
    # 风控时重新拉取 Cookie 的等待上限（秒）
    cookie_refresh_timeout: float = 60.0
    # 风控重试：最多尝试解析的次数（含首次）与每次重试前的等待（秒）
    risk_retry_attempts: int = DEFAULT_RISK_ATTEMPTS
    risk_retry_interval: float = DEFAULT_RISK_INTERVAL
    # 请求节流（防频率风控）：最小请求间隔与随机抖动比例
    request_min_interval: float = 1.0
    request_jitter_ratio: float = 0.5
    # 代理（防 IP 维度风控）；空 = 直连
    proxy: str = ""

    def is_watched(self, message_type: str, target_id: int) -> bool:
        """该会话是否在白名单内。"""
        if message_type == "group":
            return target_id in self.group_ids
        if message_type == "private":
            return target_id in self.private_ids
        return False

    def describe_listen(self) -> str:
        groups = ", ".join(str(i) for i in sorted(self.group_ids)) or "无"
        privates = ", ".join(str(i) for i in sorted(self.private_ids)) or "无"
        return f"群聊 [{groups}] / 私聊 [{privates}]"


def _as_float(value, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name} 必须是数字，收到 {value!r}")


def _as_int(value, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name} 必须是整数，收到 {value!r}")


def load_config(config_path: str = DEFAULT_CONFIG_FILE,
                allow_path: str = None) -> BotConfig:
    """读取 config.json + allow.txt。

    :param allow_path: 白名单路径；默认与 config.json 同目录的 allow.txt
    :raises ConfigError: 任一文件缺失 / 格式非法
    """
    if not os.path.isfile(config_path):
        raise ConfigError(f"配置文件不存在：{config_path}")
    try:
        # utf-8-sig：容忍 Windows 编辑器/PowerShell 写入的 BOM
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件 JSON 格式错误：{e}")
    if not isinstance(data, dict):
        raise ConfigError("config.json 根节点必须是 JSON 对象")

    cfg = BotConfig(config_path=os.path.abspath(config_path))

    napcat = data.get("napcat") or {}
    if not isinstance(napcat, dict):
        raise ConfigError("napcat 必须是 JSON 对象")
    cfg.ws_url = str(napcat.get("ws_url") or cfg.ws_url)
    cfg.token = str(napcat.get("token") or "")
    cfg.reconnect_interval = _as_float(
        napcat.get("reconnect_interval"), "napcat.reconnect_interval", 5.0)
    cfg.read_timeout = _as_float(
        napcat.get("read_timeout"), "napcat.read_timeout", 90.0)

    if allow_path is None:
        allow_path = os.path.join(
            os.path.dirname(cfg.config_path) or ".", DEFAULT_ALLOW_FILE)
    cfg.allow_path = os.path.abspath(allow_path)
    entries, private_ids, group_ids = load_allow_file(cfg.allow_path)
    cfg.listen = entries
    cfg.private_ids = private_ids
    cfg.group_ids = group_ids

    cfg.download_dir = str(data.get("download_dir") or "downloads")
    cfg.keep_files = bool(data.get("keep_files", False))
    cfg.process_timeout = _as_float(
        data.get("process_timeout"), "process_timeout", 300.0)
    cfg.cookie_refresh_timeout = _as_float(
        data.get("cookie_refresh_timeout"), "cookie_refresh_timeout", 60.0)
    cfg.risk_retry_attempts = max(1, _as_int(
        data.get("risk_retry_attempts"), "risk_retry_attempts",
        DEFAULT_RISK_ATTEMPTS))
    cfg.risk_retry_interval = max(0.0, _as_float(
        data.get("risk_retry_interval"), "risk_retry_interval",
        DEFAULT_RISK_INTERVAL))
    cfg.request_min_interval = max(0.0, _as_float(
        data.get("request_min_interval"), "request_min_interval", 1.0))
    cfg.request_jitter_ratio = max(0.0, _as_float(
        data.get("request_jitter_ratio"), "request_jitter_ratio", 0.5))
    cfg.proxy = str(data.get("proxy") or "").strip()

    messages = dict(DEFAULT_MESSAGES)
    raw_messages = data.get("messages")
    if raw_messages is not None:
        if not isinstance(raw_messages, dict):
            raise ConfigError("messages 必须是 JSON 对象")
        for key, value in raw_messages.items():
            if not isinstance(value, str):
                raise ConfigError(f"messages.{key} 必须是字符串")
            messages[str(key)] = value
    cfg.messages = messages
    return cfg
