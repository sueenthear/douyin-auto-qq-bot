# -*- coding: utf-8 -*-
"""QQ bot 入口：连接 NapCat，监听白名单内的会话，处理抖音分享链接。

配置拆成两个持久化文件（默认都在项目根目录）：
    config.json   接入信息（NapCat 地址 / token / 下载目录等）
    allow.txt     监听白名单：一行一个，# 群号 / * 私聊号，// 开头为注释

用法：
    python -m qq_bot
    python -m qq_bot --config config.json --allow allow.txt --check
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .config import ConfigError, load_config
from .handler import DouyinQQBot
from .napcat import NapCatClient

DEFAULT_CONFIG = "config.json"
DEFAULT_ALLOW = "allow.txt"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m qq_bot",
        description="抖音分享链接 → QQ 自动解析机器人（NapCat / OneBot 11）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "config.json（接入信息）：\n"
            "{\n"
            '  "napcat": { "ws_url": "ws://127.0.0.1:3001/", "token": "123456" },\n'
            '  "download_dir": "downloads",\n'
            '  "keep_files": false\n'
            "}\n\n"
            "allow.txt（监听白名单，一行一个）：\n"
            "// 注释行（// 开头整行忽略）\n"
            "*100000001\n"
            "#200000003\n"
        ),
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"接入配置文件路径（默认 {DEFAULT_CONFIG}）")
    ap.add_argument("--allow", default=None,
                    help=f"白名单文件路径（默认与 config 同目录的 {DEFAULT_ALLOW}）")
    ap.add_argument("--check", action="store_true",
                    help="仅校验配置并测试连接，不进入监听")
    return ap


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config, args.allow)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    _log(f"接入配置：{cfg.config_path}")
    _log(f"白名单　：{cfg.allow_path}（{len(cfg.listen)} 条）")
    _log(f"NapCat　：{cfg.ws_url}")
    _log(f"监听　　：{cfg.describe_listen()}")

    client = NapCatClient(cfg.ws_url, cfg.token, on_event=None,
                          reconnect_interval=cfg.reconnect_interval,
                          read_timeout=cfg.read_timeout,
                          logger=_log)
    bot = DouyinQQBot(cfg, client, logger=_log)

    if args.check:
        try:
            client.connect()
            info = client.get_login_info()
            _log(f"连接成功：{info.get('user_id')} ({info.get('nickname')})")
            _log(f"在线状态：{client.get_status()}")
            client.close()
            _log("配置与连接校验通过。")
            return 0
        except Exception as e:
            print(f"连接失败：{e}", file=sys.stderr)
            return 1

    def on_connected() -> None:
        info = client.get_login_info()
        bot.self_id = info.get("user_id") or bot.self_id
        _log(f"已登录：{bot.self_id} ({info.get('nickname')})")
        client.on_event = bot.on_event

    _log("开始监听（Ctrl+C 退出）")
    try:
        client.run_forever(on_connected=on_connected)
    except KeyboardInterrupt:
        _log("收到中断信号")
    finally:
        client.close()
        _log(f"统计：{bot.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
