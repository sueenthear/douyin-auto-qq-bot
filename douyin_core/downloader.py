# -*- coding: utf-8 -*-
"""
downloader.py — 文件流式下载模块
支持：进度回调、断点续传（可选）、限速（可选）
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable, Optional

import requests

CHUNK_SIZE = 256 * 1024  # 256 KB

ProgressCallback = Callable[[int, int, float], None]
# 回调参数：downloaded_bytes, total_bytes(-1 表示未知), speed_bps


class DownloadError(Exception):
    pass


def safe_filename(name: str, max_len: int = 80) -> str:
    """把任意字符串转为 Windows 安全文件名。

    除 Windows 保留字符外，还须清洗 ``#`` 与 ``%`` ——
    文件名会经 ``to_file_uri()`` 拼成 ``file:///`` 交给协议端，
    而协议端用 ``fileURLToPath()`` 按 URI 规则解析：

    * ``#`` 是 fragment 分隔符，会被当作 URL 片段截断路径
      （``…\\眠眠羊毛衫_#厚黑….mp4`` → ``…\\眠眠羊毛衫_``），报 ENOENT
    * ``%`` 会触发 URI 解码，非法序列直接抛 ``URI malformed``

    ``?`` 同样会被截断，但它已在 Windows 保留字符里。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t#%]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        name = "video"
    return name[:max_len]


def build_filename(info, item_id: str = "") -> str:
    """根据解析结果生成下载文件名：作者_标题_ID.mp4"""
    parts = [safe_filename(info.author or "未知作者"),
             safe_filename(info.title or "无标题"),
             info.item_id or item_id]
    return "_".join(p for p in parts if p) + ".mp4"


def build_image_filename(info, index: int, ext: str = "jpeg") -> str:
    """图集作品单张图片的文件名：作者_标题_ID_序号.ext"""
    parts = [safe_filename(info.author or "未知作者"),
             safe_filename(info.title or "无标题"),
             info.item_id or "photo"]
    return "_".join(p for p in parts if p) + f"_{index + 1:02d}.{ext}"


def download_file(
    url: str,
    dest_dir: str,
    filename: str,
    headers: Optional[dict] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = 30.0,
    resume: bool = True,
    url_fallbacks: Optional[list] = None,
) -> str:
    """流式下载文件；主地址失败时自动尝试 url_fallbacks 中的备用地址。

    :return: 最终文件完整路径
    :raises DownloadError: 全部地址下载失败 / 被取消
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    tmp = dest + ".part"

    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ))
    req_headers.setdefault("Referer", "https://www.douyin.com/")

    errors: list[str] = []
    for index, candidate in enumerate([url] + list(url_fallbacks or [])):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadError("下载已取消")
        try:
            return _download_one(candidate, dest, tmp, req_headers,
                                 on_progress, cancel_event, timeout, resume)
        except DownloadError as e:
            errors.append(f"地址 {index + 1}：{e}")
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadError("下载已取消")
    raise DownloadError("；".join(errors))


def _download_one(url, dest, tmp, req_headers, on_progress, cancel_event,
                  timeout, resume) -> str:
    """单个地址的下载实现（内部辅助）。"""
    resume_from = 0
    headers = dict(req_headers)
    if resume and os.path.exists(tmp):
        resume_from = os.path.getsize(tmp)
        headers["Range"] = f"bytes={resume_from}-"

    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
    except requests.RequestException as e:
        raise DownloadError(f"请求失败：{e}")

    if resp.status_code == 416:  # Range 无效（服务端不支持续传）
        resume_from = 0
        headers.pop("Range", None)
        resp = requests.get(url, headers=headers, stream=True, timeout=timeout)

    if resp.status_code not in (200, 206):
        resp.close()
        raise DownloadError(f"HTTP {resp.status_code}")

    total = -1
    content_range = resp.headers.get("Content-Range")
    if content_range and "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            total = -1
    elif resp.headers.get("Content-Length"):
        total = int(resp.headers["Content-Length"]) + resume_from

    mode = "ab" if (resume_from and resp.status_code == 206) else "wb"
    downloaded = resume_from if mode == "ab" else 0

    try:
        with open(tmp, mode) as f:
            start = time.time()
            last_report = start
            last_bytes = downloaded
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadError("下载已取消")
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if on_progress and (now - last_report >= 0.1 or downloaded >= total):
                    speed = (downloaded - last_bytes) / max(now - last_report, 1e-6)
                    on_progress(downloaded, total, speed)
                    last_report, last_bytes = now, downloaded
    finally:
        resp.close()

    if on_progress:
        on_progress(downloaded, total, 0.0)

    if os.path.exists(dest):
        os.remove(dest)
    os.replace(tmp, dest)
    return dest


def format_size(num: float) -> str:
    """字节数 → 人类可读，如 12.5 MB"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GB"


def format_speed(bps: float) -> str:
    return format_size(bps) + "/s"
