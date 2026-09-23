# -*- coding: utf-8 -*-
"""qq_bot.config 的离线单元测试：config.json + allow.txt 拆分加载。"""

import json

import pytest

from qq_bot.config import (
    ConfigError,
    load_allow_file,
    load_config,
    parse_allow_entry,
)

# ---------------------------------------------------------------- 条目解析

def test_parse_group_entry():
    assert parse_allow_entry("#200000003") == ("group", 200000003)


def test_parse_private_entry():
    assert parse_allow_entry("*100000001") == ("private", 100000001)


def test_parse_entry_trims_whitespace():
    assert parse_allow_entry("  *100000001  ") == ("private", 100000001)


def test_parse_entry_rejects_missing_prefix():
    with pytest.raises(ConfigError, match="必须以"):
        parse_allow_entry("100000001")


def test_parse_entry_rejects_non_digit():
    with pytest.raises(ConfigError, match="必须是数字"):
        parse_allow_entry("*abc")


def test_parse_entry_rejects_empty():
    with pytest.raises(ConfigError):
        parse_allow_entry("   ")


# ---------------------------------------------------------------- BOM 容忍

def test_allow_file_tolerates_utf8_bom(tmp_path):
    """Windows 编辑器/PowerShell 写入 BOM 时，首个条目不能被 \\ufeff 破坏。"""
    p = tmp_path / "allow.txt"
    p.write_bytes("\ufeff*100000001\n".encode("utf-8"))
    entries, privates, _ = load_allow_file(str(p))
    assert entries == ["*100000001"]
    assert privates == {100000001}


def test_config_tolerates_utf8_bom(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_bytes('\ufeff{"napcat": {"ws_url": "ws://x/"}}'.encode("utf-8"))
    allow = tmp_path / "allow.txt"
    allow.write_text("*111\n", encoding="utf-8")
    loaded = load_config(str(cfg), str(allow))
    assert loaded.ws_url == "ws://x/"


# ---------------------------------------------------------------- allow.txt

def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_allow_file_basic(tmp_path):
    p = _write(tmp_path / "allow.txt", "*100000001\n#200000003\n")
    entries, privates, groups = load_allow_file(p)
    assert entries == ["*100000001", "#200000003"]
    assert privates == {100000001}
    assert groups == {200000003}


def test_allow_file_ignores_comment_and_blank_lines(tmp_path):
    p = _write(tmp_path / "allow.txt", (
        "// 这是注释\n"
        "\n"
        "   \n"
        "//#9999999999\n"
        "*100000001\n"
        "// 尾部注释\n"
    ))
    entries, privates, groups = load_allow_file(p)
    assert entries == ["*100000001"]
    assert privates == {100000001}
    assert groups == set()


def test_allow_file_dedupes(tmp_path):
    p = _write(tmp_path / "allow.txt", "*111\n*111\n#222\n")
    entries, privates, groups = load_allow_file(p)
    assert privates == {111}
    assert groups == {222}
    assert entries == ["*111", "*111", "#222"]  # 原始条目保序保留


def test_allow_file_reports_line_number(tmp_path):
    p = _write(tmp_path / "allow.txt", "*111\n坏行\n")
    with pytest.raises(ConfigError, match="第 2 行"):
        load_allow_file(p)


def test_allow_file_missing(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_allow_file(str(tmp_path / "nope.txt"))


def test_allow_file_empty_raises(tmp_path):
    p = _write(tmp_path / "allow.txt", "// 只有注释\n\n")
    with pytest.raises(ConfigError, match="白名单为空"):
        load_allow_file(p)


# ---------------------------------------------------------------- config.json

def _make_config(tmp_path, allow_text="*100000001\n", config=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config or {
        "napcat": {"ws_url": "ws://127.0.0.1:3001/", "token": "123456",
                   "reconnect_interval": 5, "read_timeout": 90},
        "download_dir": "downloads",
        "keep_files": False,
    }, ensure_ascii=False), encoding="utf-8")
    allow_path = tmp_path / "allow.txt"
    allow_path.write_text(allow_text, encoding="utf-8")
    return str(cfg_path), str(allow_path)


def test_load_config_reads_both_files(tmp_path):
    cfg_path, allow_path = _make_config(tmp_path)
    cfg = load_config(cfg_path, allow_path)
    assert cfg.ws_url == "ws://127.0.0.1:3001/"
    assert cfg.token == "123456"
    assert cfg.reconnect_interval == 5.0
    assert cfg.read_timeout == 90.0
    assert cfg.download_dir == "downloads"
    assert cfg.keep_files is False
    assert cfg.private_ids == {100000001}
    assert cfg.group_ids == set()
    assert cfg.is_watched("private", 100000001) is True
    assert cfg.is_watched("private", 12345) is False
    assert cfg.is_watched("group", 100000001) is False


def test_load_config_defaults_allow_next_to_config(tmp_path):
    cfg_path, _ = _make_config(tmp_path)
    cfg = load_config(cfg_path)          # 不传 allow_path
    assert cfg.allow_path.endswith("allow.txt")
    assert cfg.private_ids == {100000001}


def test_load_config_reports_missing_allow(tmp_path):
    cfg_path, _ = _make_config(tmp_path)
    (tmp_path / "allow.txt").unlink()
    with pytest.raises(ConfigError, match="白名单文件不存在"):
        load_config(cfg_path)


def test_load_config_rejects_bad_json(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON 格式错误"):
        load_config(str(bad))


def test_load_config_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(str(tmp_path / "nope.json"))


def test_load_config_rejects_non_numeric_interval(tmp_path):
    cfg_path, allow_path = _make_config(tmp_path, config={
        "napcat": {"ws_url": "ws://x/", "reconnect_interval": "abc"},
    })
    with pytest.raises(ConfigError, match="必须是数字"):
        load_config(cfg_path, allow_path)


def test_load_config_keeps_default_messages_and_allows_override(tmp_path):
    cfg_path, allow_path = _make_config(tmp_path, config={
        "messages": {"video": "自定义视频文案"},
    })
    cfg = load_config(cfg_path, allow_path)
    assert cfg.messages["video"] == "自定义视频文案"
    assert "图文" in cfg.messages["image"]      # 未覆盖的仍是默认
