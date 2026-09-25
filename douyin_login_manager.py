# -*- coding: utf-8 -*-
"""douyin_login_manager.py — 抖音登录态管理 CLI（纯命令行，无界面）

功能：
  1. 查看基本信息：凭据文件、Cookie 关键字段（脱敏）、在线登录状态、浏览器 profile
  2. 手动刷新 Cookie：复用已登录的浏览器 profile，免扫码重新拉取
  3. 打开浏览器登录：扫码 / 账号密码（切换账号、或 profile 被清空后使用）
  4. 清空本地持久化：
       `clear`        只删 cookies.json —— 保留浏览器 profile，重登免扫码
       `clear --all`  连 .browser_profile 一起删 —— **切换账号必须**，
                      否则浏览器会复用旧账号登录态
  5. 链接解析测试：输出作品基本信息与下载直链，**不做实际下载**

用法（子命令）：
    python douyin_login_manager.py info
    python douyin_login_manager.py info --no-verify
    python douyin_login_manager.py refresh
    python douyin_login_manager.py login
    python douyin_login_manager.py clear
    python douyin_login_manager.py clear --all --yes
    python douyin_login_manager.py parse "<分享文案或链接>"
    python douyin_login_manager.py parse "<链接>" --json
    python douyin_login_manager.py                    # 无参数 → 交互菜单

说明：仅用标准输入输出交互，不依赖 tkinter 等任何界面库。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import unicodedata

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from douyin_core import downloader, douyin_api, login_manager  # noqa: E402
from douyin_core.__main__ import info_to_dict, resolve_cookie  # noqa: E402
from douyin_core.douyin_parser import DouyinParser, ParseError  # noqa: E402
from douyin_core.login_manager import (                       # noqa: E402
    CookieStore,
    LoginManager,
    has_session_cookie,
    verify_cookie_online,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

DEFAULT_CONFIG = "config.json"

# 展示用的关键 Cookie 字段（值一律脱敏后输出）
INFO_COOKIE_KEYS = (
    "sessionid", "sessionid_ss", "sid_guard", "uid_tt", "uid_tt_ss",
    "passport_csrf_token", "odin_tt", "ttwid", "msToken", "s_v_web_id",
)


# ---------------------------------------------------------------- 输出工具


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文输出前统一改为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _display_width(text: str) -> int:
    """按显示列宽计算（中文/全角算 2 列），保证控制台对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _title(text: str, file=None) -> None:
    stream = file or sys.stdout
    print(file=stream)
    print("=" * 64, file=stream)
    print(f"  {text}", file=stream)
    print("=" * 64, file=stream)


def _print_rows(pairs) -> None:
    """按显示宽度对齐输出 key : value。"""
    width = max((_display_width(str(k)) for k, _ in pairs), default=0)
    for key, value in pairs:
        print(f"{_pad(str(key), width)} : {value}")


# ---------------------------------------------------------------- 读取工具


def _mask(value: str) -> str:
    """Cookie 值脱敏：只保留前 4 个字符与长度。"""
    if not value:
        return "（空）"
    if len(value) <= 4:
        return f"****（len={len(value)}）"
    return f"{value[:4]}…（len={len(value)}）"


def _parse_cookie(cookie: str) -> dict:
    """Cookie 字符串 → {name: value}。"""
    jar: dict = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        jar[name.strip()] = value.strip()
    return jar


def _read_cookie_meta(path: str) -> dict:
    """读取 cookies.json 的元信息（saved_at / host）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dir_stats(path: str) -> tuple:
    """目录统计：返回 (文件数, 总字节数)；不存在返回 (0, 0)。"""
    if not os.path.isdir(path):
        return 0, 0
    count = 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                continue
    return count, total


def _profile_dir() -> str:
    """浏览器 profile 目录（与 login_manager 内部保持一致，避免路径漂移）。"""
    return os.path.join(os.path.dirname(os.path.abspath(login_manager.__file__)),
                        ".browser_profile")


def apply_runtime_config(config_path: str = DEFAULT_CONFIG) -> dict:
    """读取 config.json 的防风控参数（请求节流 / 代理）并应用到 douyin_api。

    :return: 摘要 dict；文件缺失或非法时 applied=False
    """
    path = os.path.join(PROJECT_DIR, config_path)
    result = {"path": path, "applied": False, "min_interval": None,
              "jitter_ratio": None, "proxy": ""}
    try:
        # utf-8-sig：容忍 Windows 编辑器写入的 BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(data, dict):
        return result

    def _num(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    min_interval = max(0.0, _num(data.get("request_min_interval"), 1.0))
    jitter_ratio = max(0.0, _num(data.get("request_jitter_ratio"), 0.5))
    proxy = str(data.get("proxy") or "").strip()
    douyin_api.configure_rate_limit(min_interval, jitter_ratio)
    douyin_api.configure_proxy(proxy)
    result.update({"applied": True, "min_interval": min_interval,
                   "jitter_ratio": jitter_ratio, "proxy": proxy})
    return result


# ---------------------------------------------------------------- 1. 基本信息


def cmd_info(args) -> int:
    store = CookieStore()
    profile = _profile_dir()
    cookie = store.load()
    exists = os.path.isfile(store.path)
    meta = _read_cookie_meta(store.path) if exists else {}

    _title("抖音登录态 · 基本信息")
    rows = [
        ("凭据文件", store.path),
        ("文件状态", (f"存在（{downloader.format_size(os.path.getsize(store.path))}）"
                      if exists else "不存在")),
    ]
    if meta.get("saved_at"):
        rows.append(("保存时间", str(meta.get("saved_at"))))
    if meta.get("host"):
        rows.append(("记录域名", str(meta.get("host"))))
    rows.append(("Cookie 长度", f"{len(cookie)} 字符" if cookie else "无"))
    rows.append(("会话凭证", "有" if has_session_cookie(cookie) else "无"))
    if not cookie:
        rows.append(("在线校验", "未执行（无 Cookie）"))
    elif args.no_verify:
        rows.append(("在线校验", "已跳过（--no-verify）"))
    else:
        ok = verify_cookie_online(cookie, timeout=args.timeout)
        rows.append(("在线校验", "已登录" if ok else "未登录 / 已失效"))
    _print_rows(rows)

    jar = _parse_cookie(cookie)
    print()
    print("关键 Cookie 字段（值已脱敏）")
    known = [(name, jar[name]) for name in INFO_COOKIE_KEYS if name in jar]
    if known:
        _print_rows([(f"  {name}", _mask(value)) for name, value in known])
    else:
        print("  （无关键字段）")
    others = sorted(set(jar) - set(INFO_COOKIE_KEYS))
    if others:
        print(f"  另有 {len(others)} 个字段：{', '.join(others)}")

    count, total = _dir_stats(profile)
    print()
    _print_rows([
        ("浏览器 profile", profile),
        ("profile 状态", (f"存在（{count} 个文件，{downloader.format_size(total)}）"
                          if count else "不存在（下次登录需重新扫码）")),
    ])
    if count:
        print("  提示：该目录内含浏览器登录态，切换账号时需用 "
              "`clear --all` 一并清空。")
    return EXIT_OK


# ---------------------------------------------------------------- 2. 刷新 Cookie


def cmd_refresh(args) -> int:
    store = CookieStore()
    _title("手动刷新 Cookie")

    old = store.load()
    print(f"刷新前：{'有 Cookie（%d 字符）' % len(old) if old else '无 Cookie'}")
    print("将复用浏览器 profile 重新拉取 Cookie（profile 内已有登录态时无需扫码）…")
    print()

    manager = LoginManager(store)
    cookie = manager.refresh_cookie_sync(
        timeout=args.timeout,
        progress_cb=lambda m: print(f"  … {m}"),
        keep_browser=args.keep_browser)

    if not cookie:
        print()
        print("刷新失败：未取到登录凭证。")
        print("处理：确认本机已安装 Edge/Chrome；若 profile 已被清空，"
              "请改用 `login` 命令扫码登录。")
        return EXIT_FAIL

    print()
    print(f"刷新成功：Cookie 已保存（{len(cookie)} 字符）")
    print(f"凭据文件：{store.path}")
    ok = verify_cookie_online(cookie, timeout=args.timeout)
    print(f"在线校验：{'已登录' if ok else '未登录（Cookie 可能已失效）'}")
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------- 3. 浏览器登录


def cmd_login(args) -> int:
    store = CookieStore()
    _title("打开浏览器登录")
    print(f"凭据将保存到：{store.path}")
    print("将打开 Edge/Chrome 并复用 .browser_profile。")
    print("若需要切换账号或 profile 已被清空，请在打开的页面中扫码登录。")
    print()

    manager = LoginManager(store)
    cookie = manager.refresh_cookie_sync(
        timeout=args.timeout,
        progress_cb=lambda m: print(f"  … {m}"),
        keep_browser=not args.close_browser)

    if not cookie:
        print()
        print("登录未完成：未取到登录凭证。")
        print(f"处理：确认浏览器已打开并完成扫码；或调大 --timeout"
              f"（当前 {args.timeout:.0f}s）。")
        return EXIT_FAIL

    print()
    print(f"登录成功：Cookie 已保存（{len(cookie)} 字符）")
    print(f"凭据文件：{store.path}")
    ok = verify_cookie_online(cookie, timeout=min(args.timeout, 30.0))
    print(f"在线校验：{'已登录' if ok else '未登录（请确认登录是否已完成）'}")
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------- 4. 清空持久化


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    """二次确认。非交互环境且未指定 --yes 时一律取消（安全默认）。"""
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} (y/N) ").strip().lower()
    except EOFError:
        print("非交互环境且未指定 --yes，已取消。")
        return False
    return answer in ("y", "yes")


def _remove_file(path: str) -> bool:
    if not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except OSError as e:
        print(f"  删除失败：{e}")
        return False


def _remove_dir(path: str) -> bool:
    if not os.path.exists(path):
        return True
    # profile 内有上万个文件，逐项删除可能因浏览器占用而失败
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        print("  删除未完成：可能有浏览器进程正占用该目录，"
              "请关闭浏览器后重试。")
        return False
    return True


def cmd_clear(args) -> int:
    store = CookieStore()
    profile = _profile_dir()
    with_profile = bool(getattr(args, "all", False))

    _title("清空本地持久化")
    targets = [("Cookie 凭据", store.path)]
    if with_profile:
        targets.append(("浏览器 profile", profile))
    _print_rows(targets)
    print()

    if with_profile:
        print("说明：将同时清空浏览器 profile，其中的登录态会一并丢失，"
              "之后必须重新扫码登录。")
    else:
        print("说明：只清空 Cookie，保留浏览器 profile —— 重新登录时通常无需扫码。")
        print("      如需切换账号，请改用 `clear --all`（连浏览器登录态一起清空）。")

    if not _confirm("确认清空以上内容？", assume_yes=args.yes):
        print("已取消，未做任何修改。")
        return EXIT_OK

    print()
    ok = _remove_file(store.path)
    print(f"  [{'OK' if ok else 'FAIL'}] Cookie 凭据：{store.path}")
    if with_profile:
        removed = _remove_dir(profile)
        print(f"  [{'OK' if removed else 'FAIL'}] 浏览器 profile：{profile}")
        ok = ok and removed

    print()
    print("后续操作：")
    if with_profile:
        print("  python douyin_login_manager.py login     # 打开浏览器扫码登录新账号")
    else:
        print("  python douyin_login_manager.py refresh   # 复用 profile 免扫码刷新")
        print("  python douyin_login_manager.py login     # 打开浏览器重新登录")
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------- 5. 解析测试


def _print_links(info) -> None:
    """输出下载直链（仅地址，不下载）。"""
    if info.is_image_post:
        print()
        print(f"下载直链（图集，共 {info.image_count} 张）")
        for index, item in enumerate(info.images, 1):
            print(f"  [{index:02d} 主] {item.url}")
            for fb_index, url in enumerate(item.fallbacks, 1):
                print(f"  [{index:02d} 备{fb_index}] {url}")
        return

    print()
    print("下载直链（视频）")
    print(f"  [主] {info.play_url or '（未取到无水印地址）'}")
    for index, url in enumerate(info.play_url_fallbacks, 1):
        print(f"  [备{index}] {url}")


def _info_rows(info) -> list:
    if info.is_image_post:
        return [
            ("类型", "图文（图集）"),
            ("作品 ID", info.item_id),
            ("标题", info.title or "（无标题）"),
            ("作者", info.author or "未知"),
            ("发布时间", info.create_time or "未知"),
            ("图片数量 / 分辨率", f"{info.image_count} 张 / {info.resolution_text}"),
            ("点赞 / 评论 / 收藏 / 分享",
             f"{info.digg_count} / {info.comment_count} / "
             f"{info.collect_count} / {info.share_count}"),
        ]
    return [
        ("类型", "视频"),
        ("作品 ID", info.item_id),
        ("标题", info.title or "（无标题）"),
        ("作者", info.author or "未知"),
        ("发布时间", info.create_time or "未知"),
        ("时长 / 分辨率", f"{info.duration_text} / {info.resolution_text}"),
        ("点赞 / 评论 / 收藏 / 分享",
         f"{info.digg_count} / {info.comment_count} / "
         f"{info.collect_count} / {info.share_count}"),
    ]


def cmd_parse(args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        try:
            text = input("请输入抖音分享文案或链接：").strip()
        except EOFError:
            text = ""
    if not text:
        print("未提供分享文案或链接。", file=sys.stderr)
        return EXIT_USAGE

    # --json 时全部诊断信息走 stderr，保证 stdout 是干净的 JSON
    diag = sys.stderr if args.json else sys.stdout

    if not args.no_throttle:
        cfg = apply_runtime_config()
        if cfg["applied"]:
            print(f"已应用 config.json 防风控参数：最小间隔 {cfg['min_interval']}s / "
                  f"抖动 {cfg['jitter_ratio']} / 代理 {cfg['proxy'] or '直连'}",
                  file=diag)

    _title("链接解析测试（只输出信息与直链，不做下载）", file=diag)
    parser = DouyinParser(cookie=resolve_cookie(args.cookie), timeout=args.timeout)
    try:
        info = parser.parse_text(text)
    except ParseError as e:
        print(f"解析失败：{e}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as e:  # 兜底
        print(f"未知错误：{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL

    if args.json:
        print(json.dumps(info_to_dict(info), ensure_ascii=False, indent=2))
        return EXIT_OK
    _print_rows(_info_rows(info))
    _print_links(info)
    print()
    print("（以上仅为地址，未执行下载；如需下载："
          "python -m douyin_core \"<链接>\" --download DIR）")
    return EXIT_OK


# ---------------------------------------------------------------- 交互菜单


MENU = (
    "  1) 查看基本信息（登录状态 / Cookie 字段 / 浏览器 profile）\n"
    "  2) 手动刷新 Cookie（复用已登录 profile，免扫码）\n"
    "  3) 打开浏览器登录（扫码 / 账号密码）\n"
    "  4) 清空 Cookie（保留浏览器 profile，重登免扫码）\n"
    "  5) 完全清空（Cookie + 浏览器 profile，切换账号用）\n"
    "  6) 链接解析测试（输出信息与下载直链，不下载）\n"
    "  0) 退出\n"
)


def interactive() -> int:
    """无参数运行时的菜单式交互（stdin 结束时直接退出）。"""
    while True:
        _title("抖音登录态管理（无界面 CLI）")
        print(MENU)
        try:
            choice = input("请选择 [0-6]：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("（非交互环境，退出）")
            return EXIT_OK

        if choice in ("0", "q", "quit", "exit"):
            print("已退出。")
            return EXIT_OK
        try:
            if choice == "1":
                cmd_info(argparse.Namespace(no_verify=False, timeout=15.0))
            elif choice == "2":
                cmd_refresh(argparse.Namespace(timeout=60.0, keep_browser=False))
            elif choice == "3":
                cmd_login(argparse.Namespace(timeout=300.0, close_browser=False))
            elif choice == "4":
                cmd_clear(argparse.Namespace(all=False, yes=False))
            elif choice == "5":
                cmd_clear(argparse.Namespace(all=True, yes=False))
            elif choice == "6":
                try:
                    text = input("请输入抖音分享文案或链接（直接回车返回）：").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if not text:
                    print("（未输入内容，返回菜单）")
                    continue
                cmd_parse(argparse.Namespace(text=[text], json=False, cookie="",
                                             timeout=15.0, no_throttle=False))
            else:
                print("无效选项，请重新输入。")
        except KeyboardInterrupt:
            print()
            print("（已中断当前操作，返回菜单）")


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python douyin_login_manager.py",
        description="抖音登录态管理 CLI：查看信息 / 刷新 Cookie / 清空持久化 / 解析测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python douyin_login_manager.py info\n"
            "  python douyin_login_manager.py refresh\n"
            "  python douyin_login_manager.py login\n"
            "  python douyin_login_manager.py clear --all --yes   # 切换账号\n"
            "  python douyin_login_manager.py parse \"<分享文案或链接>\"\n"
            "  python douyin_login_manager.py                     # 交互菜单\n"
        ),
    )
    sub = ap.add_subparsers(dest="command", metavar="命令")

    p = sub.add_parser("info", help="查看登录状态 / Cookie 字段 / 浏览器 profile")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过在线登录校验（离线可用）")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="在线校验超时秒数（默认 15）")

    p = sub.add_parser("refresh", help="复用浏览器 profile 手动刷新 Cookie")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="等待登录凭证的秒数（默认 60）")
    p.add_argument("--keep-browser", action="store_true",
                   help="保留浏览器窗口（默认取到 Cookie 后关闭）")

    p = sub.add_parser("login", help="打开浏览器登录（扫码 / 账号密码）")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="等待扫码登录的秒数（默认 300）")
    p.add_argument("--close-browser", action="store_true",
                   help="登录完成后关闭浏览器窗口（默认保留，便于查看结果）")

    p = sub.add_parser("clear", help="清空本地持久化（切换账号 / 重登）")
    p.add_argument("--all", action="store_true",
                   help="连同浏览器 profile 一起清空（切换账号必须）")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认提示")

    p = sub.add_parser("parse", help="链接解析测试：输出信息与下载直链（不下载）")
    p.add_argument("text", nargs="*", help="抖音分享文案或链接")
    p.add_argument("--json", action="store_true", help="以 JSON 输出解析结果")
    p.add_argument("--cookie", default="",
                   help="显式指定 Cookie（默认读 cookies.json）")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="单请求超时秒数（默认 15）")
    p.add_argument("--no-throttle", action="store_true",
                   help="忽略 config.json 的节流 / 代理设置")

    return ap


def main(argv=None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)

    if not args.command:
        return interactive()

    handlers = {
        "info": cmd_info,
        "refresh": cmd_refresh,
        "login": cmd_login,
        "clear": cmd_clear,
        "parse": cmd_parse,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        print("已中断。")
        raise SystemExit(130)
