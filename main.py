# -*- coding: utf-8 -*-
"""
main.py — douyin-auto-qq-bot 入口脚本

职责（按顺序）：
  1. 自检：尝试导入并启动 douyin_core，验证解析/签名/下载模块可用
  2. 登录自检：读取 cookies.json，检查是否含会话凭证并在线校验
  3. 登录失效时：清空 Cookie（cookies.json），并引导重新登录

用法：
    python main.py                     # 自检 + 登录检测（失效则清空并询问是否重登）
    python main.py --login             # 检测失效时直接打开浏览器重登
    python main.py --no-clear          # 只检测，不清空 Cookie
    python main.py --check-only        # 仅自检与登录检测，不进入交互
    python main.py --parse "<分享链接>"  # 自检通过后直接解析
"""

from __future__ import annotations

import argparse
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

EXIT_OK = 0
EXIT_FAIL = 1


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，统一改为 UTF-8 以正常输出中文/emoji。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


# ------------------------------------------------------------ 1. 自检


def selftest() -> bool:
    """尝试启动 douyin_core：导入包并验证关键模块可用。"""
    print("=" * 60)
    print("步骤 1/2：douyin_core 自检")
    print("=" * 60)

    try:
        import douyin_core
    except Exception as e:
        _fail(f"导入 douyin_core 失败：{e}")
        return False
    _ok(f"douyin_core 导入成功（v{getattr(douyin_core, '__version__', '?')}）")

    ok = True

    # 链接提取
    try:
        from douyin_core import extract_share_url
        sample = "2.35 复制打开抖音 https://v.douyin.com/vo_5dUe23Oc/ o@d.Nj"
        got = extract_share_url(sample)
        if got == "https://v.douyin.com/vo_5dUe23Oc/":
            _ok("分享链接提取正常")
        else:
            _fail(f"分享链接提取异常：{got!r}")
            ok = False
    except Exception as e:
        _fail(f"链接提取模块异常：{e}")
        ok = False

    # 解析器构造
    try:
        from douyin_core import DouyinParser
        DouyinParser()
        _ok("解析器 DouyinParser 可实例化")
    except Exception as e:
        _fail(f"解析器不可用：{e}")
        ok = False

    # 签名模块（a_bogus 优先，X-Bogus 兜底）
    try:
        from douyin_core.douyin_api import sign_url
        signed, _ua = sign_url(
            "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1&aid=6383")
        if "a_bogus=" in signed:
            _ok("签名模块可用（a_bogus / gmssl）")
        elif "X-Bogus=" in signed:
            _warn("a_bogus 不可用，已回退 X-Bogus（建议 pip install gmssl）")
        else:
            _fail("签名模块未产生签名参数")
            ok = False
    except Exception as e:
        _fail(f"签名模块异常：{e}")
        ok = False

    # 下载模块
    try:
        from douyin_core import downloader
        if downloader.safe_filename('a/b:c*d?"e') == "a_b_c_d__e":
            _ok("下载模块可用（文件名清洗正常）")
        else:
            _warn("下载模块文件名清洗行为异常")
    except Exception as e:
        _fail(f"下载模块异常：{e}")
        ok = False

    # selenium（登录流程依赖，缺失时登录管理器会自动安装）
    try:
        import selenium  # noqa: F401
        _ok(f"selenium 可用（登录流程就绪）")
    except ImportError:
        _warn("selenium 未安装，登录时会自动 pip 安装")

    return ok


# ------------------------------------------------------------ 2. 登录自检


def check_login(clear_on_fail: bool = True) -> tuple:
    """检查登录状态。

    :return: (是否已登录, 是否已清空 Cookie)
    """
    print()
    print("=" * 60)
    print("步骤 2/2：登录状态自检")
    print("=" * 60)

    from douyin_core.login_manager import (
        CookieStore, has_session_cookie, verify_cookie_online,
    )

    store = CookieStore()
    print(f"  凭据文件：{store.path}")

    cookie = store.load()
    if not cookie:
        _warn("未找到 Cookie（尚未登录）")
        return False, False

    _ok(f"已读取 Cookie（{len(cookie)} 字符）")

    if not has_session_cookie(cookie):
        _fail("Cookie 中不含登录会话凭证（sessionid 等），判定为未登录")
    elif not verify_cookie_online(cookie):
        _fail("Cookie 在线校验失败：登录已失效")
    else:
        _ok("Cookie 在线校验通过：已登录")
        return True, False

    if not clear_on_fail:
        _warn("按 --no-clear 要求保留现有 Cookie")
        return False, False

    store.clear()
    print(f"  [清理] 已删除失效凭据：{store.path}")
    return False, True


# ------------------------------------------------------------ 3. 重登引导


def prompt_relogin(auto: bool = False) -> bool:
    """引导用户重新登录（清空 Cookie 后调用）。返回是否成功登录。"""
    print()
    print("-" * 60)
    print("Cookie 已清空，需要重新登录抖音。")
    print("登录方式：自动打开本机 Edge/Chrome，扫码或账号密码登录后自动抓取 Cookie。")
    print("-" * 60)

    if not auto:
        try:
            answer = input("是否现在打开浏览器登录？(Y/n) ").strip().lower()
        except EOFError:
            print("（非交互环境，跳过自动登录。稍后可运行：python main.py --login）")
            return False
        if answer and answer not in ("y", "yes"):
            print("已跳过。稍后可运行：python main.py --login")
            return False

    from douyin_core.__main__ import do_login
    return do_login() == EXIT_OK


# ------------------------------------------------------------ 主流程


def main(argv=None) -> int:
    _force_utf8_stdout()

    ap = argparse.ArgumentParser(
        prog="python main.py",
        description="douyin-auto-qq-bot 入口：douyin_core 自检 + 登录状态检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py                      # 自检 + 登录检测\n"
            "  python main.py --login              # 失效时直接打开浏览器重登\n"
            "  python main.py --no-clear           # 只检测，不清空 Cookie\n"
            "  python main.py --parse \"https://v.douyin.com/xxx/\"\n"
        ),
    )
    ap.add_argument("--login", action="store_true",
                    help="登录失效时直接打开浏览器重登（不再询问）")
    ap.add_argument("--no-clear", action="store_true",
                    help="登录失效时保留 Cookie，不执行清空")
    ap.add_argument("--check-only", action="store_true",
                    help="仅自检与登录检测，不做任何清理或交互")
    ap.add_argument("--parse", metavar="TEXT",
                    help="自检通过后解析该分享文案/链接")
    ap.add_argument("--download", metavar="DIR",
                    help="配合 --parse 使用，解析成功后下载到该目录")
    args = ap.parse_args(argv)

    if not selftest():
        print()
        print("自检未通过，请先修复上述 [FAIL] 项。")
        return EXIT_FAIL

    logged_in, _cleared = check_login(
        clear_on_fail=not (args.no_clear or args.check_only))

    if not logged_in:
        if args.check_only:
            print()
            print("自检完成（未登录）。运行 `python main.py --login` 可登录。")
            return EXIT_FAIL
        # 未登录时（无论原本无凭据、还是检测失效后已清空）都应能触发登录
        if not prompt_relogin(auto=args.login):
            print()
            print("提示：登录完成后可运行 `python main.py` 重新检查状态。")
            return EXIT_FAIL
        # 登录后复检
        ok, _ = check_login(clear_on_fail=False)
        if not ok:
            print()
            print("登录后仍未通过校验，请重试。")
            return EXIT_FAIL

    if args.parse:
        print()
        print("=" * 60)
        print("解析")
        print("=" * 60)
        from douyin_core.__main__ import main as core_main
        core_argv = [args.parse]
        if args.download:
            core_argv += ["--download", args.download]
        return core_main(core_argv)

    print()
    print("就绪：douyin_core 可用，登录态正常。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
