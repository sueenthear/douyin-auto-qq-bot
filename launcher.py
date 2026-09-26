# -*- coding: utf-8 -*-
"""launcher.py — 一键自检并启动机器人；任一依赖挂掉时暂停等待人工处理。

自检项：
  1. douyin_core 是否可用（解析内核能否导入）
  2. 抖音登录态是否有效（cookies.json + 在线校验）
  3. NapCat 是否可连接（config.json 里的 ws_url / token）

运行期监控：
  * NapCat 连接断开          → 暂停，提示后按回车重新自检并启动
  * 抖音登录态失效            → 暂停，提示后按回车重新自检并启动

用法：
    python launcher.py                 # 自检 + 启动 + 监控
    python launcher.py --check         # 只自检，不启动
    python launcher.py --no-login-watch   # 不做运行期登录态巡检
"""

from __future__ import annotations

import argparse
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from qq_bot.config import ConfigError, load_config          # noqa: E402
from qq_bot.handler import DouyinQQBot                      # noqa: E402
from qq_bot.napcat import NapCatClient                      # noqa: E402

DEFAULT_CONFIG = "config.json"
DEFAULT_WATCH_INTERVAL = 5.0        # NapCat 连接巡检间隔（秒）
DEFAULT_LOGIN_INTERVAL = 120.0      # 抖音登录态巡检间隔（秒）


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pause(reason: str, wait_seconds: float = 10.0) -> bool:
    """暂停并等待用户按回车。

    :return: True = 用户确认继续；False = 非交互环境（stdin 已结束），
             调用方应退出，避免无人值守时无限循环卡死。
    """
    print()
    print("=" * 64)
    lines = str(reason).splitlines()
    for index, line in enumerate(lines):
        print(f"  暂停：{line}" if index == 0 else f"        {line}")
    print("=" * 64)
    print("  处理完成后按回车继续（Ctrl+C 退出）")
    try:
        input("> ")
        return True
    except EOFError:
        log(f"非交互环境（stdin 已结束）：{wait_seconds:.0f} 秒后重试一次")
        time.sleep(wait_seconds)
        return False


# ---------------------------------------------------------------- 自检


def check_douyin_core() -> list:
    """检查解析内核是否可用。"""
    problems = []
    try:
        import douyin_core  # noqa: F401
        from douyin_core import DouyinParser, extract_share_url  # noqa: F401
        from douyin_core.login_manager import (  # noqa: F401
            CookieStore, has_session_cookie, verify_cookie_online,
        )
    except Exception as e:
        return [f"douyin_core 不可用：{e}",
                "处理：确认已安装依赖（pip install -r requirements.txt）"]
    log("自检 1/3：douyin_core 可用")

    cookie = CookieStore().load()
    if not cookie:
        problems.append("未登录抖音：douyin_core/cookies.json 不存在")
        problems.append("处理：运行 python main.py --login")
    elif not has_session_cookie(cookie):
        problems.append("Cookie 中不含登录凭证（sessionid 等）")
        problems.append("处理：运行 python main.py --login")
    elif not verify_cookie_online(cookie):
        problems.append("抖音登录已失效")
        problems.append("处理：运行 python main.py --login")
    else:
        log("自检 2/3：抖音登录态正常")
    return problems


def check_napcat(cfg) -> list:
    """检查协议端（NapCat / SnowLuma）是否可连接，并按 config 解析后端。"""
    client = NapCatClient(cfg.ws_url, cfg.token, logger=lambda m: None)
    try:
        client.connect()
        info = client.get_login_info()
        status = client.get_status()
        if cfg.backend == "auto":
            cfg.detected_backend = client.detect_backend() or "napcat"
        log(f"自检 3/3：协议端正常（{info.get('user_id')} "
            f"{info.get('nickname')}，online={status.get('online')}）")
        log(f"         协议端：{cfg.backend_label()}")
        return []
    except Exception as e:
        return [f"协议端连接失败（{cfg.ws_url}）：{e}",
                "处理：确认 NapCat / SnowLuma 已启动，"
                "且 config.json 里的 ws_url / token 正确"]
    finally:
        client.close()


def selftest(cfg) -> list:
    """返回问题列表（空表示全部通过）。"""
    problems = check_douyin_core()
    problems += check_napcat(cfg)
    return problems


# ---------------------------------------------------------------- 运行


def check_douyin_login() -> bool:
    """运行期登录态巡检（网络异常时保守返回 True）。"""
    try:
        from douyin_core.login_manager import (
            CookieStore, has_session_cookie, verify_cookie_online,
        )
    except Exception:
        return True
    cookie = CookieStore().load()
    if not cookie or not has_session_cookie(cookie):
        return False
    return verify_cookie_online(cookie)


def run_session(cfg, watch_interval: float, login_interval: float,
                watch_login: bool) -> str:
    """连接 NapCat、启动机器人并监控；返回问题描述（正常退出返回 ""）。"""
    client = NapCatClient(cfg.ws_url, cfg.token, logger=log)
    try:
        client.connect()
    except Exception as e:
        return (f"协议端连接失败：{e}\n"
                "处理：确认 NapCat / SnowLuma 已启动后重试")

    info = client.get_login_info()
    if cfg.backend == "auto":
        cfg.detected_backend = client.detect_backend() or "napcat"
    bot = DouyinQQBot(cfg, client, self_id=info.get("user_id") or 0, logger=log)
    client.on_event = bot.on_event

    log(f"已连接协议端：{info.get('user_id')} ({info.get('nickname')})")
    log(f"实际协议端：{cfg.backend_label()}")
    log(f"监听：{cfg.describe_listen()}")
    log("机器人已启动（Ctrl+C 退出）")

    last_login_check = time.time()
    try:
        while True:
            time.sleep(watch_interval)
            if not client.connected:
                return ("协议端连接已断开\n"
                        "处理：确认 NapCat / SnowLuma 进程与网络端口后重试")
            if watch_login and time.time() - last_login_check >= login_interval:
                last_login_check = time.time()
                if not check_douyin_login():
                    return ("抖音登录已失效（解析将全部失败）\n"
                            "处理：运行 python main.py --login 重新登录")
    except KeyboardInterrupt:
        log("收到中断信号")
        return ""
    finally:
        client.close()
        log(f"本次统计：{bot.stats}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python launcher.py",
        description="一键自检并启动抖音→QQ 机器人；依赖挂掉时暂停等待处理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "自检项：douyin_core 可用性 / 抖音登录态 / NapCat 连接\n"
            "运行期监控：NapCat 连接断开、抖音登录失效 → 暂停，回车后重新自检并启动\n"
        ),
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"接入配置文件（默认 {DEFAULT_CONFIG}）")
    ap.add_argument("--allow", default=None,
                    help="白名单文件（默认与 config 同目录的 allow.txt）")
    ap.add_argument("--check", action="store_true",
                    help="只做自检，不启动机器人")
    ap.add_argument("--watch-interval", type=float, default=DEFAULT_WATCH_INTERVAL,
                    help=f"NapCat 连接巡检间隔秒数（默认 {DEFAULT_WATCH_INTERVAL:g}）")
    ap.add_argument("--login-interval", type=float, default=DEFAULT_LOGIN_INTERVAL,
                    help=f"抖音登录态巡检间隔秒数（默认 {DEFAULT_LOGIN_INTERVAL:g}）")
    ap.add_argument("--no-login-watch", action="store_true",
                    help="关闭运行期登录态巡检")
    return ap


def main(argv=None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)

    log(f"项目目录：{PROJECT_DIR}")
    try:
        cfg = load_config(args.config, args.allow)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    log(f"接入配置：{cfg.config_path}")
    log(f"白名单　：{cfg.allow_path}（{len(cfg.listen)} 条）")
    log(f"协议端　：{cfg.backend}")

    if args.check:
        problems = selftest(cfg)
        if problems:
            print()
            print("自检未通过：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print()
        log("自检全部通过。")
        return 0

    while True:
        problems = selftest(cfg)
        if problems:
            if not pause("\n".join(problems) + "\n处理完成后回车重新自检"):
                log("非交互环境，退出。修复后重新运行本程序。")
                return 1
            continue

        reason = run_session(cfg, args.watch_interval, args.login_interval,
                             not args.no_login_watch)
        if not reason:
            log("已退出。")
            return 0
        if not pause(reason + "\n处理完成后回车重新自检并启动"):
            log("非交互环境，退出。处理后可重新运行本程序。")
            return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        log("已退出。")
        raise SystemExit(0)
