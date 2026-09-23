# -*- coding: utf-8 -*-
"""
douyin_core 命令行入口（最小 CLI，无任何界面依赖）

用法：
    python -m douyin_core "<分享文案或链接>"              # 解析并打印信息
    python -m douyin_core "<分享文案或链接>" --json       # 输出 JSON
    python -m douyin_core "<分享文案或链接>" --download D  # 解析后下载到目录 D
    python -m douyin_core --login                         # 打开浏览器登录并保存 Cookie
    python -m douyin_core --check-login                   # 检测当前 Cookie 是否有效
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

from .douyin_parser import DouyinParser, ParseError, VideoInfo
from .login_manager import CookieStore, LoginManager, LoginState
from . import downloader

EXIT_OK = 0
EXIT_PARSE_ERROR = 1
EXIT_USAGE = 2


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文/emoji 输出前统一改为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def resolve_cookie(explicit: str = "") -> str:
    """按优先级取 Cookie：显式参数 → 环境变量 DOUYIN_COOKIE → cookies.json。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("DOUYIN_COOKIE", "").strip()
    if env:
        return env
    return CookieStore().load()


def info_to_dict(info: VideoInfo) -> dict:
    """VideoInfo → 可 JSON 序列化的 dict（不含 raw 大对象）。"""
    return {
        "item_id": info.item_id,
        "title": info.title,
        "author": info.author,
        "create_time": info.create_time,
        "media_type": info.media_type,
        "duration_sec": info.duration_sec,
        "width": info.width,
        "height": info.height,
        "play_url": info.play_url,
        "play_url_fallbacks": list(info.play_url_fallbacks),
        "cover_url": info.cover_url,
        "digg_count": info.digg_count,
        "comment_count": info.comment_count,
        "collect_count": info.collect_count,
        "share_count": info.share_count,
        "image_count": info.image_count,
        "images": [
            {"url": im.url, "fallbacks": list(im.fallbacks),
             "width": im.width, "height": im.height, "ext": im.ext}
            for im in info.images
        ],
    }


def print_info(info: VideoInfo) -> None:
    if info.is_image_post:
        rows = [
            ("类型", "图文（图集）"),
            ("作品 ID", info.item_id),
            ("标题", info.title or "（无标题）"),
            ("作者", info.author or "未知"),
            ("发布时间", info.create_time or "未知"),
            ("图片数量 / 分辨率",
             f"{info.image_count} 张 / {info.resolution_text}"),
            ("点赞 / 评论 / 收藏 / 分享",
             f"{info.digg_count} / {info.comment_count} / "
             f"{info.collect_count} / {info.share_count}"),
        ]
        width = max(len(k) for k, _ in rows)
        for key, value in rows:
            print(f"{key.ljust(width)} : {value}")
        print()
        for i, im in enumerate(info.images):
            print(f"图片 {i + 1:02d} [{im.resolution_text}] : {im.url}")
        return

    rows = [
        ("类型", "视频"),
        ("作品 ID", info.item_id),
        ("标题", info.title or "（无标题）"),
        ("作者", info.author or "未知"),
        ("发布时间", info.create_time or "未知"),
        ("时长 / 分辨率", f"{info.duration_text} / {info.resolution_text}"),
        ("点赞 / 评论 / 收藏 / 分享",
         f"{info.digg_count} / {info.comment_count} / "
         f"{info.collect_count} / {info.share_count}"),
        ("无水印地址", info.play_url or "（获取失败）"),
        ("备用地址数", str(len(info.play_url_fallbacks))),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key.ljust(width)} : {value}")


def do_login(timeout: float = 300.0) -> int:
    """走登录管理器的一键登录流程（打开本机浏览器，扫码后自动抓取 Cookie）。"""
    manager = LoginManager()
    finished = threading.Event()
    result: dict = {}

    def on_result(state: LoginState, msg: str) -> None:
        print(f"[{state.value}] {msg}")
        result["state"] = state
        if state in (LoginState.LOGGED_IN, LoginState.NOT_LOGGED):
            finished.set()

    manager.start_login_async(on_result, progress_cb=lambda m: print(f"… {m}"))
    finished.wait(timeout=timeout + 30)
    if result.get("state") == LoginState.LOGGED_IN:
        print(f"Cookie 已保存至：{manager.store.path}")
        return EXIT_OK
    print("登录未完成。")
    return EXIT_PARSE_ERROR


def do_check_login(cookie: str) -> int:
    from .login_manager import has_session_cookie, verify_cookie_online

    store = CookieStore()
    current = resolve_cookie(cookie)
    if not current:
        print(f"未找到 Cookie（{store.path} 不存在或为空）")
        return EXIT_PARSE_ERROR
    if not has_session_cookie(current):
        print("Cookie 中不含登录会话凭证（sessionid 等），视为未登录。")
        return EXIT_PARSE_ERROR
    if verify_cookie_online(current):
        print("已登录（Cookie 校验通过）")
        return EXIT_OK
    print("登录已失效，请重新登录。")
    return EXIT_PARSE_ERROR


def do_parse(args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("请提供分享文案或链接。", file=sys.stderr)
        return EXIT_USAGE

    parser = DouyinParser(cookie=resolve_cookie(args.cookie), timeout=args.timeout)
    try:
        info = parser.parse_text(text)
    except ParseError as e:
        print(f"解析失败：{e}", file=sys.stderr)
        return EXIT_PARSE_ERROR
    except Exception as e:  # 兜底
        print(f"未知错误：{e}", file=sys.stderr)
        return EXIT_PARSE_ERROR

    if args.json:
        print(json.dumps(info_to_dict(info), ensure_ascii=False, indent=2))
    else:
        print_info(info)

    if args.download:
        if info.is_image_post:
            return _download_images(info, args.download)
        if not info.play_url:
            print("无可用播放地址，跳过下载。", file=sys.stderr)
            return EXIT_PARSE_ERROR
        filename = downloader.build_filename(info)
        last = {"t": 0.0}

        def on_progress(done: int, total: int, speed: float) -> None:
            now = time.time()
            if now - last["t"] < 0.5 and (total <= 0 or done < total):
                return
            last["t"] = now
            if total and total > 0:
                pct = min(100.0, done / total * 100)
                print(f"\r下载中 {pct:5.1f}%  "
                      f"{downloader.format_size(done)} / "
                      f"{downloader.format_size(total)}  "
                      f"{downloader.format_speed(speed)}   ", end="")
            else:
                print(f"\r已下载 {downloader.format_size(done)}  "
                      f"{downloader.format_speed(speed)}   ", end="")

        try:
            path = downloader.download_file(
                info.play_url, args.download, filename,
                on_progress=on_progress,
                url_fallbacks=info.play_url_fallbacks,
            )
        except downloader.DownloadError as e:
            print(f"\n下载失败：{e}", file=sys.stderr)
            return EXIT_PARSE_ERROR
        print(f"\n已保存：{path}")
    return EXIT_OK


def _download_images(info: VideoInfo, dest_dir: str) -> int:
    """逐张下载图集作品的图片。"""
    total = info.image_count
    print(f"开始下载图集（共 {total} 张）…")
    saved = failed = 0
    for i, im in enumerate(info.images):
        filename = downloader.build_image_filename(info, i, im.ext)
        try:
            path = downloader.download_file(
                im.url, dest_dir, filename, url_fallbacks=im.fallbacks)
        except downloader.DownloadError as e:
            failed += 1
            print(f"  第 {i + 1:02d} 张失败：{e}", file=sys.stderr)
            continue
        saved += 1
        print(f"  [{i + 1:02d}/{total}] 已保存：{path}")
    print(f"完成：成功 {saved} 张，失败 {failed} 张")
    return EXIT_OK if failed == 0 else EXIT_PARSE_ERROR


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m douyin_core",
        description="抖音无水印视频解析内核（纯逻辑，无 UI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m douyin_core \"2.35 复制打开抖音... https://v.douyin.com/xxx/ ...\"\n"
            "  python -m douyin_core https://v.douyin.com/xxx/ --json\n"
            "  python -m douyin_core https://v.douyin.com/xxx/ --download ./out\n"
            "  python -m douyin_core --login\n"
        ),
    )
    ap.add_argument("text", nargs="*", help="抖音分享文案或链接")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出解析结果")
    ap.add_argument("--download", metavar="DIR", help="解析成功后下载到该目录")
    ap.add_argument("--cookie", default="", help="显式指定 Cookie 字符串")
    ap.add_argument("--cookie-file", default="", help="从文件读取 Cookie（覆盖默认 cookies.json）")
    ap.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒数（默认 15）")
    ap.add_argument("--login", action="store_true", help="打开浏览器登录并保存 Cookie")
    ap.add_argument("--check-login", action="store_true", help="检测当前 Cookie 是否有效")
    return ap


def main(argv=None) -> int:
    _force_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cookie_file:
        args.cookie = CookieStore(args.cookie_file).load()
    if args.login:
        return do_login()
    if args.check_login:
        return do_check_login(args.cookie)
    return do_parse(args)


if __name__ == "__main__":
    raise SystemExit(main())
