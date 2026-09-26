# -*- coding: utf-8 -*-
"""qq_bot.handler 的离线单元测试（不联网、不连 NapCat）。"""

import json

from douyin_core.douyin_parser import ImageItem, VideoInfo

from qq_bot.handler import build_image_nodes, format_info, guess_media_type

# ---------------------------------------------------------------- 类型预判

def test_guess_media_type_image():
    assert guess_media_type("看看【大梦觉义录的图文作品】陀螺2") == "image"


def test_guess_media_type_video():
    assert guess_media_type("看看【暖笙小憨猪的作品】为粉丝爆肝1个月") == "video"


def test_guess_media_type_unknown():
    assert guess_media_type("https://v.douyin.com/xxx/") == "unknown"
    assert guess_media_type("") == "unknown"
    assert guess_media_type(None) == "unknown"


# ---------------------------------------------------------------- 合并转发节点

def test_image_nodes_caption_once_then_only_images():
    """首个节点只放文案，其后每个节点只放一张图片（文案不重复）。"""
    paths = [r"C:\a\1.jpeg", r"C:\a\2.jpeg", r"C:\a\3.jpeg"]
    nodes = build_image_nodes(1922747401, "某作者", "标题文案", paths)

    assert len(nodes) == 1 + len(paths)      # 1 个文案节点 + N 个图片节点

    first = nodes[0]
    assert first["type"] == "node"
    assert first["data"]["uin"] == "1922747401"
    assert first["data"]["name"] == "某作者"
    assert [seg["type"] for seg in first["data"]["content"]] == ["text"]
    assert first["data"]["content"][0]["data"]["text"] == "标题文案"

    for index, node in enumerate(nodes[1:]):
        content = node["data"]["content"]
        assert [seg["type"] for seg in content] == ["image"]
        # 关键：后续节点不含文案
        assert "标题文案" not in json.dumps(content, ensure_ascii=False)
        assert paths[index].replace("\\", "/") in content[0]["data"]["file"]


def test_image_nodes_uses_percent_encoded_file_uri():
    """图片路径必须是 file:/// + 正斜杠，并按 RFC 8089 做百分号编码。

    早期按 NapCat 实测结论「不做编码」，但 SnowLuma 走 Node 的
    ``fileURLToPath()`` 按 URI 规则解析：不编码时 ``#`` 会被当作 fragment
    截断路径、``%`` 会抛 URI malformed，导致本地文件发送失败（ENOENT）。
    中文等非 ASCII 字符必须编码，否则 SnowLuma 还原不出原路径。
    """
    nodes = build_image_nodes(1, "作者", "标题", [r"C:\目录\图 1.jpeg"])
    uri = nodes[1]["data"]["content"][0]["data"]["file"]
    assert uri.startswith("file:///C:/")
    # 中文与空格均须编码，协议端才能还原
    assert "%E7%9B%AE%E5%BD%95" in uri        # 目录
    assert "%E5%9B%BE" in uri                 # 图
    assert "%20" in uri                       # 空格
    assert " " not in uri


def test_image_nodes_encodes_hash_in_filename():
    """回归：文件名含 # 时不得原样出现在 URI 里（会被当作 URL fragment 截断）。"""
    nodes = build_image_nodes(1, "作者", "标题",
                              [r"C:\a\眠眠羊毛衫_#厚黑_1.jpeg"])
    uri = nodes[1]["data"]["content"][0]["data"]["file"]
    assert "#" not in uri
    assert "%23" in uri


def test_image_nodes_author_fallback():
    nodes = build_image_nodes(1, "", "标题", [r"C:\a\1.jpeg"])
    assert nodes[0]["data"]["name"] == "抖音"


def test_image_nodes_single_image():
    nodes = build_image_nodes(1, "作者", "标题", [r"C:\a\1.jpeg"])
    assert len(nodes) == 2


def test_image_nodes_long_title_is_clipped():
    nodes = build_image_nodes(1, "作者", "标" * 900, [r"C:\a\1.jpeg"])
    text = nodes[0]["data"]["content"][0]["data"]["text"]
    assert len(text) <= 501
    assert text.endswith("…")


# ---------------------------------------------------------------- 视频缩略图

def _make_video_bot(cover_url="https://p3.douyinpic.com/cover.jpeg"):
    """构造一个不连 NapCat 的 bot，用于测 _download_thumb。"""
    from qq_bot.handler import DouyinQQBot

    bot = DouyinQQBot.__new__(DouyinQQBot)
    bot.log = lambda m: None
    return bot, VideoInfo(item_id="123", cover_url=cover_url)


def test_download_thumb_returns_none_without_cover(tmp_path):
    bot, info = _make_video_bot(cover_url="")
    assert bot._download_thumb(info, str(tmp_path)) is None


def test_download_thumb_uses_cover_ext(tmp_path, monkeypatch):
    """缩略图扩展名应从封面 URL 推断（保证 NapCat 能识别格式）。"""
    from douyin_core import downloader

    seen = {}

    def fake_download(url, dest_dir, filename, **kw):
        seen["url"] = url
        seen["filename"] = filename
        return str(tmp_path / filename)

    monkeypatch.setattr(downloader, "download_file", fake_download)
    bot, info = _make_video_bot(
        cover_url="https://p3.douyinpic.com/cover.webp?x=1")
    path = bot._download_thumb(info, str(tmp_path))
    assert path is not None
    assert seen["filename"].endswith(".webp")      # 跟封面格式一致
    assert seen["url"] == info.cover_url


def test_download_thumb_handles_failure(tmp_path, monkeypatch):
    from douyin_core import downloader

    def boom(*a, **k):
        raise downloader.DownloadError("403")

    monkeypatch.setattr(downloader, "download_file", boom)
    bot, info = _make_video_bot()
    assert bot._download_thumb(info, str(tmp_path)) is None


# ---------------------------------------------------------------- info 文本

def test_format_info_image_post():
    # width/height 由 _build_info 从首图回填，这里按真实构造方式补齐
    info = VideoInfo(item_id="1", title="标题", author="作者",
                     width=940, height=1564,
                     images=[ImageItem(url="u", width=940, height=1564)],
                     media_type="image", digg_count=1, comment_count=2,
                     collect_count=3, share_count=4)
    text = format_info(info)
    assert text.startswith("【抖音图文】")
    assert "图片：1 张 / 940x1564" in text
    assert "赞 1 · 评 2 · 藏 3 · 转 4" in text


def test_format_info_video_post():
    info = VideoInfo(item_id="1", title="标题", author="作者",
                     duration_sec=213, width=1440, height=1080,
                     play_url="http://x")
    text = format_info(info)
    assert text.startswith("【抖音视频】")
    assert "时长/分辨率：03:33 / 1440x1080" in text
