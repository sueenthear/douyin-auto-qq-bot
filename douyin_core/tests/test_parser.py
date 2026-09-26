# -*- coding: utf-8 -*-
"""douyin_parser 与 downloader 的离线单元测试（不需要网络）。"""

import pytest

from douyin_core.douyin_parser import (
    DouyinParser,
    ParseError,
    VideoInfo,
    _find_item_id_in_url,
    extract_share_url,
    make_watermark_free,
)
from douyin_core import downloader
from douyin_core.douyin_api import DouyinAPIError

# 用户提供的示例分享文案（繁体 + 短链 + 尾部杂字符）
SAMPLE_TEXT = (
    "2.35 复制打开抖音，看看【元帝寶的作品】被電鑽過肩摔  "
    "https://v.douyin.com/vo_5dUe23Oc/ o@d.Nj ytr:/ 04/14 :3pm"
)


# ---------------------------------------------------------------- 链接提取

def test_extract_share_from_sample():
    assert extract_share_url(SAMPLE_TEXT) == "https://v.douyin.com/vo_5dUe23Oc/"


def test_extract_web_video_url():
    url = "https://www.douyin.com/video/7412345678901234567"
    assert extract_share_url(f"看看这个 {url} 好看") == url


def test_extract_ies_share_url():
    url = "https://www.iesdouyin.com/share/video/7412345678901234567/"
    assert extract_share_url(url) == url


def test_extract_none_when_no_url():
    assert extract_share_url("今天天气不错，没有链接") is None
    assert extract_share_url("") is None


def test_extract_url_with_trailing_punctuation():
    text = "链接是 https://v.douyin.com/abc123/，快看"
    assert extract_share_url(text) == "https://v.douyin.com/abc123/"


def test_extract_first_of_many():
    text = "第一个 https://v.douyin.com/aaa/ 第二个 https://v.douyin.com/bbb/"
    assert extract_share_url(text) == "https://v.douyin.com/aaa/"


# ---------------------------------------------------------------- item_id

def test_item_id_variants():
    assert _find_item_id_in_url(
        "https://www.douyin.com/video/7412345678901234567") == "7412345678901234567"
    assert _find_item_id_in_url(
        "https://www.douyin.com/note/7412345678901234567") == "7412345678901234567"
    assert _find_item_id_in_url(
        "https://www.iesdouyin.com/share/video/7412345678901234567/") == "7412345678901234567"
    assert _find_item_id_in_url(
        "https://www.douyin.com/discover?modal_id=7412345678901234567") == "7412345678901234567"
    assert _find_item_id_in_url(
        "https://v.douyin.com/vo_5dUe23Oc/") is None  # 短链需跟随重定向


# ---------------------------------------------------------------- 去水印

def test_watermark_free():
    assert make_watermark_free(
        "https://www.douyin.com/aweme/v1/playwm/?video_id=abc&ratio=720p") == \
        "https://www.douyin.com/aweme/v1/play/?video_id=abc&ratio=720p"
    # 已是无水印地址则保持不变
    assert make_watermark_free(
        "https://www.douyin.com/aweme/v1/play/?video_id=abc") == \
        "https://www.douyin.com/aweme/v1/play/?video_id=abc"


# ---------------------------------------------------------------- 信息组装

API_ITEM = {
    "aweme_id": "7412345678901234567",
    "desc": "被電鑽過肩摔",
    "create_time": 1718400000,
    "author": {"nickname": "元帝寶"},
    "statistics": {"digg_count": 12345, "comment_count": 67,
                   "share_count": 89, "collect_count": 10},
    "video": {
        "duration": 15300,
        "width": 1080,
        "height": 1920,
        "play_addr": {"uri": "v0200abc", "url_list": [
            "https://www.douyin.com/aweme/v1/playwm/?video_id=v0200abc&ratio=720p"]},
        "bit_rate": [{"play_addr": {"url_list": [
            "https://www.douyin.com/aweme/v1/play/?video_id=v0200abc&ratio=1080p"]}}],
        "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.jpeg"]},
    },
}


def test_build_info_from_api_json():
    parser = DouyinParser()
    info = parser._build_info(API_ITEM, "7412345678901234567")
    assert info.item_id == "7412345678901234567"
    assert info.title == "被電鑽過肩摔"
    assert info.author == "元帝寶"
    assert info.duration_sec == 15
    assert info.duration_text == "00:15"
    assert info.resolution_text == "1080x1920"
    assert info.digg_count == 12345
    # bit_rate 中存在无水印地址时优先选用
    assert "playwm" not in info.play_url
    assert info.play_url.startswith("https://www.douyin.com/aweme/v1/play/?")


def test_build_info_falls_back_to_play_addr():
    item = dict(API_ITEM)
    item["video"] = dict(API_ITEM["video"])
    del item["video"]["bit_rate"]
    parser = DouyinParser()
    info = parser._build_info(item, "x")
    # 只有 playwm 地址时：构造签名 play 端点（watermark=0，无水印）
    assert info.play_url
    assert "playwm" not in info.play_url
    assert "aweme/v1/play/?" in info.play_url
    assert "a_bogus=" in info.play_url or "X-Bogus=" in info.play_url


# ---------------------------------------------------------------- 回退逻辑

def test_fetch_info_falls_back_to_page(monkeypatch):
    parser = DouyinParser()
    # 方案 0（官方接口）必须 mock 掉，否则会走真实网络
    monkeypatch.setattr(
        "douyin_core.douyin_parser.douyin_api.fetch_video_detail",
        lambda item_id, cookie="", **kw: (_ for _ in ()).throw(
            DouyinAPIError("官方接口不可用")))

    def fake_api(item_id):
        raise ParseError("接口不可用")

    def fake_page(item_id):
        return VideoInfo(item_id=item_id, title="来自页面",
                         play_url="https://example.com/play/?v=1")

    monkeypatch.setattr(parser, "_fetch_from_api", fake_api)
    monkeypatch.setattr(parser, "_fetch_from_page", fake_page)
    info = parser._fetch_info("123")
    assert info.title == "来自页面"
    assert info.play_url == "https://example.com/play/?v=1"


def test_fetch_info_raises_when_all_fail(monkeypatch):
    """三级回退全部失败（非风控）→ 抛 ParseError 汇总原因。"""
    parser = DouyinParser()
    monkeypatch.setattr(
        "douyin_core.douyin_parser.douyin_api.fetch_video_detail",
        lambda item_id, cookie="", **kw: (_ for _ in ()).throw(
            DouyinAPIError("官方接口失败")))
    monkeypatch.setattr(parser, "_fetch_from_api",
                        lambda i: (_ for _ in ()).throw(ParseError("a")))
    monkeypatch.setattr(parser, "_fetch_from_page",
                        lambda i: (_ for _ in ()).throw(ParseError("b")))
    with pytest.raises(ParseError, match="解析失败"):
        parser._fetch_info("123")


# ---------------------------------------------------------------- 页面 JSON

def test_extract_page_json_router_data():
    html = (
        "<html><script>window._ROUTER_DATA = "
        '{"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": []}}}}'
        "</script></html>"
    )
    payload = DouyinParser._extract_page_json(html)
    assert payload is not None
    assert "loaderData" in payload


def test_extract_page_json_none():
    assert DouyinParser._extract_page_json("<html>nothing here</html>") is None


# ---------------------------------------------------------------- 下载器

def test_safe_filename():
    assert downloader.safe_filename('a/b:c*d?"e|f') == "a_b_c_d__e_f"
    assert downloader.safe_filename("   ") == "video"
    assert downloader.safe_filename("标题 带 空格  ") == "标题 带 空格"


def test_safe_filename_strips_uri_special_chars():
    """# 与 % 必须清洗，否则拼成 file:/// 后协议端解析失败。

    # 会被当作 URL fragment 截断路径（SnowLuma 报 ENOENT）；
    % 会触发 URI 解码并抛 "URI malformed"。
    """
    # 首尾的 _ 由既有的 strip(" ._") 去掉（与 Windows 保留字符同等处理）
    assert downloader.safe_filename("#厚黑 #ootd") == "厚黑 _ootd"
    assert downloader.safe_filename("100%纯棉") == "100_纯棉"
    # 组合场景：线上真实标题
    got = downloader.safe_filename("#厚黑 #ootd穿搭拍照 #厚黑美学")
    assert "#" not in got and "%" not in got


def test_build_filename_from_hash_title_stays_uri_safe():
    """回归：带 # 标签的抖音标题不能再产出会被 URI 截断的文件名。"""
    from qq_bot.napcat import to_file_uri
    info = VideoInfo(item_id="7435226612766936370", author="眠眠羊毛衫",
                     title="#厚黑 #ootd穿搭拍照 #厚黑美学")
    name = downloader.build_filename(info)
    assert "#" not in name and "%" not in name
    uri = to_file_uri(r"C:\t\douyin_bot_x" + "\\" + name)
    assert "#" not in uri
    # 不含 # / ? 时，路径部分不应出现任何百分号编码歧义
    assert uri.startswith("file:///C:/t/douyin_bot_x/")


def test_build_filename():
    info = VideoInfo(item_id="7412345678901234567", author="元帝寶",
                     title="被電鑽過肩摔")
    name = downloader.build_filename(info)
    assert name == "元帝寶_被電鑽過肩摔_7412345678901234567.mp4"


def test_format_size():
    assert downloader.format_size(1023) == "1023 B"
    assert downloader.format_size(1024) == "1.0 KB"
    assert downloader.format_size(5 * 1024 * 1024) == "5.0 MB"


# ---------------------------------------------------------------- 图文（图集）

IMAGE_ITEM = {
    "aweme_id": "7688239828618995177",
    "desc": "陀螺2 #讽刺",
    "create_time": 1790000000,
    "author": {"nickname": "大梦觉义录"},
    "statistics": {"digg_count": 108, "comment_count": 4,
                   "share_count": 17, "collect_count": 8},
    "aweme_type": 68,
    "media_type": 2,
    "images": [
        {
            "uri": "tos-cn-i-0813c001/abc",
            "width": 940,
            "height": 1564,
            "url_list": [
                "https://p3-pc-sign.douyinpic.com/abc~tplv-dy-aweme-images:q75.webp?x=1",
                "https://p9-pc-sign.douyinpic.com/abc~tplv-dy-aweme-images:q75.webp?x=1",
                "https://p3-pc-sign.douyinpic.com/abc~tplv-dy-aweme-images:q75.jpeg?x=1",
            ],
            "download_url_list": [
                "https://p3-pc-sign.douyinpic.com/abc~tplv-dy-water-v2:abc:940:1564.webp?x=1",
            ],
        },
        {
            "uri": "tos-cn-i-0813c001/def",
            "width": 940,
            "height": 1564,
            "url_list": [
                "https://p3-pc-sign.douyinpic.com/def~tplv-dy-aweme-images:q75.jpeg?x=2",
            ],
        },
    ],
    # 图集作品的 video.play_addr 实际指向 BGM 音频（audio/mp4），
    # 绝不能当成播放地址，否则下载到的「视频」是纯黑无画面的音频文件。
    "video": {
        "duration": 0,
        "width": 720,
        "play_addr": {
            "uri": "https://sf11-cdn-tos.douyinstatic.com/obj/bgm",
            "url_list": ["https://sf11-cdn-tos.douyinstatic.com/obj/bgm"],
        },
    },
}


def test_build_info_recognizes_image_post():
    """图文作品必须识别为 image，且不能把 BGM 音频当作 play_url。"""
    parser = DouyinParser()
    info = parser._build_info(IMAGE_ITEM, "7688239828618995177")
    assert info.is_image_post is True
    assert info.media_type == "image"
    assert info.image_count == 2
    assert info.play_url == ""                 # 关键：不再误取 BGM 音频
    assert info.title == "陀螺2 #讽刺"
    assert info.author == "大梦觉义录"
    assert info.resolution_text == "940x1564"  # 取第一张图的尺寸
    assert info.cover_url                      # 封面回退到首图


def test_build_images_prefers_unwatermarked_jpeg():
    """优先无水印 url_list，且 jpeg 排在 webp 之前。"""
    parser = DouyinParser()
    imgs = parser._build_images(IMAGE_ITEM["images"])
    first = imgs[0]
    assert "tplv-dy-aweme-images" in first.url     # 无水印模板
    assert "tplv-dy-water-v2" not in first.url     # 不是带水印模板
    assert ".jpeg" in first.url                    # jpeg 优先于 webp
    assert first.ext == "jpeg"
    assert (first.width, first.height) == (940, 1564)
    # 其余地址（两个 webp 副本）进入 fallbacks
    assert len(first.fallbacks) == 2
    assert all("webp" in u for u in first.fallbacks)


def test_build_images_falls_back_to_download_url_list():
    """url_list 缺失时退回 download_url_list（带水印兜底），不丢图。"""
    parser = DouyinParser()
    imgs = parser._build_images([{
        "width": 100, "height": 200,
        "download_url_list": ["https://x.douyinpic.com/a~tplv-dy-water-v2.webp"],
    }])
    assert len(imgs) == 1
    assert imgs[0].url.endswith("tplv-dy-water-v2.webp")
    assert imgs[0].ext == "webp"


def test_build_images_skips_invalid_entries():
    parser = DouyinParser()
    assert parser._build_images(None) == []
    assert parser._build_images([None, "x", {}, {"url_list": []}]) == []


def test_fetch_info_succeeds_with_images_only(monkeypatch):
    """判据放宽：play_url 为空但 images 非空，也应视为解析成功。"""
    parser = DouyinParser()
    monkeypatch.setattr(
        "douyin_core.douyin_parser.douyin_api.fetch_video_detail",
        lambda item_id, cookie="": IMAGE_ITEM)
    info = parser._fetch_info("7688239828618995177")
    assert info.is_image_post
    assert info.image_count == 2
    assert info.item_id == "7688239828618995177"


def test_video_post_is_not_image_post():
    """回归：普通视频作品不应被误判为图文。"""
    parser = DouyinParser()
    info = parser._build_info(API_ITEM, "7412345678901234567")
    assert info.is_image_post is False
    assert info.media_type == "video"
    assert info.image_count == 0
    assert info.play_url


def test_build_image_filename():
    info = VideoInfo(item_id="7688239828618995177", author="大梦觉义录",
                     title="陀螺2")
    assert downloader.build_image_filename(info, 0, "jpeg") == \
        "大梦觉义录_陀螺2_7688239828618995177_01.jpeg"
    assert downloader.build_image_filename(info, 11, "webp") == \
        "大梦觉义录_陀螺2_7688239828618995177_12.webp"
