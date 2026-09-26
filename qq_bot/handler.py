# -*- coding: utf-8 -*-
"""抖音分享链接 → QQ 消息处理。

流程：

视频
  1. 收到视频分享链接
  2. 引用原消息回复「检测到抖音视频分享链接，正在解析中……」
  3. 发送视频 info
  4. 发送无水印视频文件

图文
  1. 收到图文分享链接
  2. 引用原消息回复「检测到抖音图文分享链接，正在解析中……」
  3. 发送图文 info
  4. 把图片（单张或多张）构建为合并转发消息并发送
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from typing import Callable, Optional

from douyin_core import DouyinParser, ParseError, downloader, extract_share_url
from douyin_core import douyin_api
from douyin_core.douyin_api import RiskControlError
from douyin_core.douyin_parser import _image_ext
from douyin_core.login_manager import CookieStore, LoginManager

from .napcat import (NapCatClient, NapCatError, build_node, image_seg,
                     text_seg, to_file_uri)

# 标题在 info 文本里的最大长度
TITLE_LIMIT = 100
# 合并转发首节点文案的上限（用户要求「显示完文案」，故放宽）
NODE_TITLE_LIMIT = 500
# 风控最终失败时的兜底文案（可在 config.json 的 messages.risk 覆盖）
DEFAULT_RISK_MESSAGE = (
    "解析失败：当前触发抖音风控（{reason}）。\n"
    "已尝试重新拉取 Cookie 仍未成功，请稍后重试。\n"
    "触发链接：{url}")


def guess_media_type(text: str) -> str:
    """从分享文案预判作品类型。

    抖音分享文案格式固定，可直接判断，无需先请求接口：
      视频 → 「看看【XXX的作品】...」
      图文 → 「看看【XXX的图文作品】...」

    :return: "video" | "image" | "unknown"
    """
    if not text:
        return "unknown"
    if "图文" in text:
        return "image"
    if "作品" in text:
        return "video"
    return "unknown"


def _clip(text: str, limit: int = TITLE_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def build_image_nodes(self_id, author: str, title: str, paths: list) -> list:
    """构造图文合并转发的节点。

    首个节点只放作品文案，其后每个节点依次只放一张图片 ——
    避免同一段文案在每张图片上重复出现。

    :param paths: 已下载到本地的图片路径（按顺序）
    """
    name = author or "抖音"
    nodes = [build_node(self_id, name, [text_seg(_clip(title, NODE_TITLE_LIMIT))])]
    for path in paths:
        nodes.append(build_node(self_id, name, [image_seg(path)]))
    return nodes


def format_info(info) -> str:
    """作品信息文本。"""
    if info.is_image_post:
        head = "【抖音图文】"
        lines = [
            f"标题：{_clip(info.title) or '（无标题）'}",
            f"作者：{info.author or '未知'}",
            f"发布：{info.create_time or '未知'}",
            f"图片：{info.image_count} 张 / {info.resolution_text}",
        ]
    else:
        head = "【抖音视频】"
        lines = [
            f"标题：{_clip(info.title) or '（无标题）'}",
            f"作者：{info.author or '未知'}",
            f"发布：{info.create_time or '未知'}",
            f"时长/分辨率：{info.duration_text} / {info.resolution_text}",
        ]
    lines.append(
        f"互动：赞 {info.digg_count} · 评 {info.comment_count} · "
        f"藏 {info.collect_count} · 转 {info.share_count}")
    return head + "\n" + "\n".join(lines)


class DouyinQQBot:
    """监听指定会话，把抖音分享链接解析成 info + 媒体文件回发。"""

    def __init__(self,
                 config,
                 client: NapCatClient,
                 self_id: int = 0,
                 logger: Optional[Callable[[str], None]] = None):
        self.config = config
        self.client = client
        self.self_id = self_id
        self.log = logger or (lambda m: None)
        # 应用防风控参数：请求节流 + 代理（从 config.json 读取）
        douyin_api.configure_rate_limit(
            getattr(config, "request_min_interval", 1.0),
            getattr(config, "request_jitter_ratio", 0.5))
        douyin_api.configure_proxy(getattr(config, "proxy", ""))
        self.parser = DouyinParser(cookie=CookieStore().load())
        self.login = LoginManager()
        # 同一时刻只允许一个刷新 Cookie 的浏览器实例
        self._refresh_lock = threading.Lock()
        self.stats = {"received": 0, "video": 0, "image": 0, "failed": 0,
                      "risk_refresh": 0}
        self._workers = set()
        self._workers_lock = threading.Lock()

    # ------------------------------------------------------------ 事件

    def on_event(self, event: dict) -> None:
        """NapCat 事件回调（在读线程里执行，必须尽快返回）。"""
        if not isinstance(event, dict) or event.get("post_type") != "message":
            return
        message_type = event.get("message_type")
        if message_type == "group":
            target_id = event.get("group_id")
        elif message_type == "private":
            target_id = event.get("user_id")
        else:
            return
        if target_id is None:
            return
        if not self.config.is_watched(message_type, target_id):
            return

        text = self._message_text(event)
        url = extract_share_url(text)
        if not url:
            return

        self.stats["received"] += 1
        self.log(f"[收到] {message_type}:{target_id} ← {url}")
        self._spawn(self._process, message_type, target_id,
                    event.get("message_id"), text)

    @staticmethod
    def _message_text(event: dict) -> str:
        """取出消息里的纯文本（数组段或字符串）。"""
        message = event.get("message")
        if isinstance(message, str):
            return message
        parts = []
        for seg in message or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text") or ""))
        return "".join(parts) or str(event.get("raw_message") or "")

    def _spawn(self, fn, *args) -> None:
        """在独立线程里跑，避免阻塞读线程。"""

        def wrapper():
            try:
                fn(*args)
            except Exception as e:
                self.log(f"[worker] 未捕获异常 {type(e).__name__}: {e}")
            finally:
                with self._workers_lock:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=wrapper, daemon=True)
        with self._workers_lock:
            self._workers.add(worker)
        worker.start()

    # ------------------------------------------------------------ 主流程

    def _process(self, message_type: str, target_id: int,
                 message_id, text: str) -> None:
        kind = guess_media_type(text)
        notice = self.config.messages.get(kind) or self.config.messages["unknown"]
        self._try_reply(message_type, target_id, message_id, notice)

        info, error = self._parse_with_risk_retry(text)
        if info is None:
            self.stats["failed"] += 1
            self.log(f"[解析失败] {error}")
            self._try_send(message_type, target_id, error)
            return

        self.log(f"[解析成功] {'图文' if info.is_image_post else '视频'} "
                 f"{info.item_id} {_clip(info.title, 40)}")
        self._try_send(message_type, target_id, format_info(info))

        work_dir, cleanup = self._make_work_dir()
        try:
            if info.is_image_post:
                self.stats["image"] += 1
                self._send_image_post(message_type, target_id, info, work_dir)
            else:
                self.stats["video"] += 1
                self._send_video_post(message_type, target_id, info, work_dir)
        finally:
            if cleanup:
                shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------ 风控重试

    def _parse_with_risk_retry(self, text: str):
        """解析作品；遇风控则重试，必要时刷新 Cookie。

        流程（按代价递增）：
          1. 直接解析 —— 大部分请求一次就成功
          2. 遇风控 → 短暂等待后**原地重试**（不开浏览器）。实测 Argus 门禁是
             概率性的：同一请求 12 次中 5 次被拦，且连续失败 3 次后重试可成功。
             重试几乎总能突破，且零额外副作用。
          3. 连续多次风控 → 才尝试「刷新 Cookie」（代价高：要开一次浏览器）
          4. 仍失败 → 回发报错并附触发链接

        早期版本第 2 步缺失：每次风控都直接开浏览器刷新 Cookie，既慢又因
        频繁启动浏览器而加重风控。

        :return: (info, "") 成功；(None, 报错文案) 失败
        """
        attempts = max(1, getattr(self.config, "risk_retry_attempts", 3))
        interval = max(0.0, getattr(self.config, "risk_retry_interval", 3.0))
        url = extract_share_url(text) or ""
        last_reason = ""
        # 连续风控达到该次数后才值得开浏览器刷新 Cookie（代价高）
        refresh_after = max(2, getattr(self.config, "cookie_refresh_after", 3))
        risk_hits = 0

        for attempt in range(1, attempts + 1):
            try:
                info = self.parser.parse_text(text)
                if attempt > 1:
                    self.log(f"[风控] 第 {attempt}/{attempts} 次尝试解析成功")
                return info, ""
            except RiskControlError as e:
                last_reason = str(e)
                risk_hits += 1
                if attempt >= attempts:
                    break

                # 先原地重试：概率性风控靠重试即可突破，且不开浏览器
                if risk_hits < refresh_after:
                    self.log(f"[风控] 第 {attempt}/{attempts} 次触发（{e}）"
                             f" —— 直接重试")
                    if interval:
                        time.sleep(interval)
                    continue

                # 连续多次风控：可能是登录态问题，这才值得刷新 Cookie
                self.log(f"[风控] 连续 {risk_hits} 次触发（{e}）"
                         f" —— 刷新 Cookie 后重试")
                if not self._refresh_cookie():
                    return None, self._risk_message(url, "刷新 Cookie 失败")
                risk_hits = 0               # 刷新后重新计数
                if interval:
                    time.sleep(interval)
            except ParseError as e:
                return None, self.config.messages["error"].format(error=e)
            except Exception as e:
                return None, self.config.messages["error"].format(
                    error=f"{type(e).__name__}: {e}")

        return None, self._risk_message(url, last_reason)

    def _risk_message(self, url: str, reason: str) -> str:
        """风控最终失败时的报错文案（含触发链接）。"""
        template = self.config.messages.get("risk") or DEFAULT_RISK_MESSAGE
        return template.format(reason=reason, url=url or "（未能提取链接）")

    def _refresh_cookie(self) -> bool:
        """启动浏览器重新拉取一次 Cookie，并同步给解析器。"""
        with self._refresh_lock:
            def progress(msg: str):
                self.log(f"[风控] {msg}")

            cookie = self.login.refresh_cookie_sync(
                timeout=self.config.cookie_refresh_timeout,
                progress_cb=progress)
        if not cookie:
            return False
        self.parser.set_cookie(cookie)
        self.stats["risk_refresh"] += 1
        return True

    # ------------------------------------------------------------ 视频

    def _send_video_post(self, message_type, target_id, info, work_dir) -> None:
        if not info.play_url:
            self._try_send(message_type, target_id, "未取到无水印播放地址。")
            return
        filename = downloader.build_filename(info)
        try:
            path = downloader.download_file(
                info.play_url, work_dir, filename,
                url_fallbacks=info.play_url_fallbacks)
        except downloader.DownloadError as e:
            self.log(f"[下载失败] {e}")
            self._try_send(message_type, target_id, f"视频下载失败：{e}")
            return
        size_mb = os.path.getsize(path) / 1024 / 1024
        self.log(f"[视频] 已下载 {size_mb:.1f} MB，开始上传…")

        # 缩略图：优先用作品封面（比 NapCat 自动抽帧更准，且不依赖其 ffmpeg）
        data = {"file": to_file_uri(path)}
        thumb = self._download_thumb(info, work_dir)
        if thumb:
            data["thumb"] = to_file_uri(thumb)
            self.log(f"[视频] 已附加缩略图：{os.path.basename(thumb)}")
        else:
            self.log("[视频] 未能取到封面，交由 NapCat 自行抽帧")

        self._try_send(message_type, target_id,
                       [{"type": "video", "data": data}], timeout=180)

    def _download_thumb(self, info, work_dir: str):
        """下载作品封面作为视频缩略图；失败返回 None。

        扩展名从封面 URL 推断（抖音封面常见 jpeg/webp），确保 NapCat 能正确识别。
        """
        if not info.cover_url:
            return None
        ext = _image_ext(info.cover_url)
        try:
            path = downloader.download_file(
                info.cover_url, work_dir, f"thumb_{info.item_id}.{ext}",
                headers={"Referer": "https://www.douyin.com/"})
            return path
        except downloader.DownloadError as e:
            self.log(f"[缩略图] 下载失败：{e}")
            return None

    # ------------------------------------------------------------ 图文

    def _send_image_post(self, message_type, target_id, info, work_dir) -> None:
        paths = []
        for index, item in enumerate(info.images):
            filename = downloader.build_image_filename(info, index, item.ext)
            try:
                paths.append(downloader.download_file(
                    item.url, work_dir, filename, url_fallbacks=item.fallbacks))
            except downloader.DownloadError as e:
                self.log(f"[图片下载失败] 第 {index + 1} 张：{e}")

        if not paths:
            self._try_send(message_type, target_id, "图片下载失败。")
            return

        total = len(paths)
        nodes = build_image_nodes(self.self_id, info.author, info.title, paths)
        self.log(f"[图文] {total} 张已下载，发送合并转发…")
        self._try_send_forward(message_type, target_id, nodes)

    # ------------------------------------------------------------ 工具

    def _make_work_dir(self):
        """返回 (目录, 是否需要清理)。"""
        if self.config.keep_files:
            path = os.path.abspath(self.config.download_dir)
            os.makedirs(path, exist_ok=True)
            return path, False
        return tempfile.mkdtemp(prefix="douyin_bot_"), True

    def _try_reply(self, message_type, target_id, message_id, text) -> None:
        try:
            self.client.reply(message_type, target_id, message_id, text)
        except NapCatError as e:
            self.log(f"[回复失败] {e}")

    def _try_send(self, message_type, target_id, message,
                  timeout: Optional[float] = None) -> None:
        try:
            self.client.send_msg(message_type, target_id, message, timeout)
        except NapCatError as e:
            self.log(f"[发送失败] {e}")

    def _try_send_forward(self, message_type, target_id, nodes) -> None:
        try:
            self.client.send_forward(message_type, target_id, nodes, timeout=120)
        except NapCatError as e:
            self.log(f"[合并转发失败] {e}")
