# -*- coding: utf-8 -*-
"""
login_manager.py — 抖音登录状态管理
功能：
  * 持久化保存 / 读取 / 清除 Cookie（cookies.json）
  * 自动检测登录状态：有 Cookie 时发起请求验证有效性
  * 一键登录：自动安装 selenium → 打开本机 Edge/Chrome → 用户扫码登录 →
    自动从浏览器抓取 Cookie 并保存
  * 全程在工作线程执行，通过回调返回结果（不直接操作 tkinter）
"""

from __future__ import annotations

import enum
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 登录会话凭证 Cookie 名（存在即视为已登录）
SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_guard", "uid_tt")

# 状态颜色（UI 层使用）：灰=未检测 黄=检测/等待中 红=未登录 绿=已登录
STATE_COLORS = {
    "unknown": "#999999",
    "checking": "#f0ad4e",
    "waiting": "#f0ad4e",
    "not_logged": "#d9534f",
    "logged_in": "#5cb85c",
}

STATE_LABELS = {
    "unknown": "未检测",
    "checking": "检测中…",
    "waiting": "等待登录…",
    "not_logged": "未登录",
    "logged_in": "已登录",
}

LOGIN_TIMEOUT_SEC = 300          # 等待用户扫码登录的最长时间
POLL_INTERVAL_SEC = 2            # 轮询 Cookie 间隔


class LoginState(str, enum.Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    WAITING = "waiting"
    NOT_LOGGED = "not_logged"
    LOGGED_IN = "logged_in"


class LoginError(Exception):
    """登录流程错误，message 面向用户可直接展示。"""


# ---------------------------------------------------------------- Cookie 存取


class CookieStore:
    def __init__(self, path: Optional[str] = None):
        if path is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "cookies.json")
        self.path = path

    def load(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookie = (data or {}).get("cookie", "")
            return cookie if isinstance(cookie, str) else ""
        except (OSError, json.JSONDecodeError):
            return ""

    def save(self, cookie: str) -> None:
        data = {
            "cookie": cookie,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "host": "douyin.com",
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- 状态检测


def has_session_cookie(cookie: str) -> bool:
    """cookie 字符串中是否存在登录会话凭证。"""
    if not cookie:
        return False
    low = cookie.lower()
    return any(name.lower() in low for name in SESSION_COOKIES)


# 登录态自检接口：返回 JSON 中 status_code==0 视为已登录；
# status_code==8 / "用户未登录" 视为未登录。该接口不要求签名，仅需 Cookie。
_PROFILE_SELF_URL = (
    "https://www.douyin.com/aweme/v1/web/user/profile/self/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
)

# 明确表示「未登录」的接口状态码
_NOT_LOGGED_STATUS_CODES = frozenset({"8", "2154"})


def verify_cookie_online(cookie: str, timeout: float = 15.0) -> bool:
    """请求登录态自检接口，验证 Cookie 是否有效。

    原先用网页 /user/self 判断：未登录时抖音同样返回 HTTP 200 的 SPA 页面，
    导致判据恒为 True（等于没判断）。改为调用 profile/self JSON 接口，
    以响应体中的 status_code 为准。

    :return: True = 已登录；False = 确定未登录。
             网络异常 / 非 JSON 响应等不确定情况返回 True（保守放行，
             避免因网络抖动误清用户凭据）。
    """
    headers = {
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get(_PROFILE_SELF_URL, headers=headers,
                            allow_redirects=False, timeout=timeout)
    except requests.RequestException:
        return True  # 网络异常无法判断，保守放行

    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if "login" in loc.lower() or "passport" in loc.lower():
            return False
        return True
    if resp.status_code != 200:
        return True  # 异常状态无法判断，保守放行

    try:
        data = resp.json()
    except ValueError:
        return True  # 非 JSON（如风控页）无法判断，保守放行
    if not isinstance(data, dict):
        return True

    status_code = str(data.get("status_code"))
    if status_code == "0" or data.get("user"):
        return True
    if status_code in _NOT_LOGGED_STATUS_CODES or "未登录" in str(
            data.get("status_msg") or ""):
        return False
    return True


# ---------------------------------------------------------------- 浏览器定位


def find_browser() -> Optional[Tuple[str, str]]:
    """返回 (类型, 可执行文件路径)。优先 Edge（Windows 自带），其次 Chrome。"""
    candidates = [
        ("edge", [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]),
        ("chrome", [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]),
    ]
    for kind, paths in candidates:
        for p in paths:
            if os.path.isfile(p):
                return kind, p
    # 兜底：PATH 中查找
    for kind, name in (("edge", "msedge"), ("chrome", "chrome")):
        p = shutil.which(name)
        if p:
            return kind, p
    return None


# ---------------------------------------------------------------- selenium 保障


def _ensure_selenium() -> Optional[str]:
    """确保 selenium 可用；不可用时自动 pip 安装。返回错误信息或 None。"""
    try:
        import selenium  # noqa: F401
        return None
    except ImportError:
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "selenium"],
            timeout=300, check=True,
        )
        import selenium  # noqa: F401
        return None
    except Exception as e:
        return f"selenium 安装失败：{e}"


def _launch_driver(kind: str, profile_dir: str):
    """按浏览器类型创建 webdriver（Selenium Manager 自动下载/匹配 driver）。

    注意：必须显式导入 webdriver 子模块（selenium 的 __getattr__ 动态导入
    无法被 PyInstaller 静态分析收集，会导致打包后 "No module named
    'selenium.webdriver.edge.webdriver'" 错误）。
    """
    common_args = [
        f"--user-data-dir={profile_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ]
    if kind == "edge":
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.webdriver import WebDriver

        opts = Options()
        for a in common_args:
            opts.add_argument(a)
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        return WebDriver(options=opts)
    else:
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.webdriver import WebDriver

        opts = Options()
        for a in common_args:
            opts.add_argument(a)
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        return WebDriver(options=opts)


# ---------------------------------------------------------------- 登录管理器


ResultCallback = Callable[[LoginState, str], None]


class LoginManager:
    """登录状态管理器（线程安全：所有操作在后台线程，结果经回调返回）。"""

    def __init__(self, store: Optional[CookieStore] = None):
        self.store = store or CookieStore()
        self.state = LoginState.UNKNOWN
        self.cookie = self.store.load()
        self._lock = threading.Lock()

    # ----- 对外 API（均异步，结果通过回调返回） -----

    def check_login_async(self, on_result: ResultCallback) -> None:
        """自动检测登录状态：无 Cookie → 未登录；有 Cookie → 在线验证。"""

        def work():
            with self._lock:
                self.state = LoginState.CHECKING
            on_result(LoginState.CHECKING, "正在检测登录状态…")
            cookie = self.store.load()
            if not has_session_cookie(cookie):
                self._set_state(LoginState.NOT_LOGGED)
                on_result(LoginState.NOT_LOGGED, "未登录（无会话 Cookie）")
                return
            valid = verify_cookie_online(cookie)
            if valid:
                self.cookie = cookie
                self._set_state(LoginState.LOGGED_IN)
                on_result(LoginState.LOGGED_IN, "已登录（Cookie 有效）")
            else:
                self._set_state(LoginState.NOT_LOGGED)
                on_result(LoginState.NOT_LOGGED, "登录已失效，请重新登录")

        threading.Thread(target=work, daemon=True).start()

    def start_login_async(self, on_result: ResultCallback,
                          progress_cb: Optional[Callable[[str], None]] = None,
                          timeout: float = LOGIN_TIMEOUT_SEC) -> None:
        """一键登录：打开浏览器 → 用户扫码/输密码 → 自动抓取 Cookie。"""

        def work():
            def progress(msg: str):
                if progress_cb:
                    progress_cb(msg)

            try:
                # 1. 保障 selenium
                progress("检查 selenium…")
                err = _ensure_selenium()
                if err:
                    raise LoginError(err)

                # 2. 定位浏览器
                browser = find_browser()
                if not browser:
                    raise LoginError("未找到 Edge/Chrome 浏览器，请安装后重试")
                kind, exe = browser
                progress(f"正在启动 {kind} 浏览器…")

                # 3. 打开抖音并等待登录
                profile_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    ".browser_profile")
                driver = _launch_driver(kind, profile_dir)
                self._set_state(LoginState.WAITING)
                on_result(LoginState.WAITING,
                          "浏览器已打开，请在页面中完成登录（扫码或账号密码）…")
                try:
                    driver.get("https://www.douyin.com/")
                    cookie = self._wait_for_login_cookie(driver, timeout)
                finally:
                    # 保留浏览器窗口，让用户看到登录结果；不强制关闭
                    pass

                if not cookie:
                    raise LoginError("等待登录超时，请重试")
                self.cookie = cookie
                self.store.save(cookie)
                self._set_state(LoginState.LOGGED_IN)
                on_result(LoginState.LOGGED_IN,
                          "登录成功，Cookie 已自动保存")
            except LoginError as e:
                self._set_state(LoginState.NOT_LOGGED)
                on_result(LoginState.NOT_LOGGED, str(e))
            except Exception as e:
                self._set_state(LoginState.NOT_LOGGED)
                on_result(LoginState.NOT_LOGGED, f"登录失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    # ----- 内部 -----

    def _wait_for_login_cookie(self, driver, timeout: float) -> str:
        """轮询浏览器 Cookie 直到出现登录凭证，返回完整 Cookie 字符串。"""
        from selenium.webdriver.support.ui import WebDriverWait
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cookies = driver.get_cookies()
            except Exception:
                cookies = []
            names = {c.get("name", "") for c in cookies}
            if any(n in SESSION_COOKIES for n in names):
                return "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                    if c.get("name") and c.get("value"))
            time.sleep(POLL_INTERVAL_SEC)
        return ""

    def _set_state(self, state: LoginState):
        with self._lock:
            self.state = state

    # ----- 同步刷新（供机器人等无界面场景使用） -----

    def refresh_cookie_sync(self, timeout: float = 60.0,
                            progress_cb: Optional[Callable[[str], None]] = None,
                            keep_browser: bool = False) -> str:
        """同步打开浏览器并重新抓取 Cookie，返回新 Cookie（失败返回空串）。

        登录使用持久化 profile（``.browser_profile``），若其中已存有登录态，
        则无需重新扫码即可直接拉到新 Cookie —— 这正是「自动重新拉取」的场景。

        :param timeout: 等待出现登录凭证的最长时间（秒）
        :param keep_browser: 是否保留浏览器窗口（默认关闭，避免堆积）
        """
        def progress(msg: str):
            if progress_cb:
                progress_cb(msg)

        try:
            err = _ensure_selenium()
            if err:
                progress(err)
                return ""
            browser = find_browser()
            if not browser:
                progress("未找到 Edge/Chrome 浏览器")
                return ""
            kind, _exe = browser
            progress(f"启动 {kind} 浏览器（复用已登录 profile）…")

            profile_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".browser_profile")
            driver = _launch_driver(kind, profile_dir)
            try:
                driver.get("https://www.douyin.com/")
                cookie = self._wait_for_login_cookie(driver, timeout)
            finally:
                if not keep_browser:
                    try:
                        driver.quit()
                    except Exception:
                        pass

            if not cookie:
                progress(f"等待 {timeout:.0f}s 未取到登录凭证")
                return ""

            self.cookie = cookie
            self.store.save(cookie)
            self._set_state(LoginState.LOGGED_IN)
            progress("已重新拉取并保存 Cookie")
            return cookie
        except Exception as e:
            progress(f"刷新 Cookie 失败：{type(e).__name__}: {e}")
            return ""


# ---------------------------------------------------------------- 便捷函数


def cookies_to_headers(cookie: str) -> dict:
    return {"User-Agent": UA, "Cookie": cookie,
            "Referer": "https://www.douyin.com/"}
