"""tools/use_key.py —— 装 key 的小工具（BUILD_SPEC 之外的运维脚本）。

它管的是凭据，所以测试重点不是「能跑」，而是三条红线：
1. 明文永不外泄（输出里只能出现掩码）；
2. 挑错了要明确拒绝，不许挑一条错的顶上；
3. 目标文件写不进去要报错退出，不许静默成功。

脚本不在包里（`tools/` 没有 `__init__.py`），按路径加载。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parents[2] / "tools" / "use_key.py"

#: 假 key（不是真凭据，只为测形态）
KEY_A = "sk-" + "a" * 32
KEY_B = "sk-" + "b" * 32


@pytest.fixture(scope="module")
def uk():
    spec = importlib.util.spec_from_file_location("use_key_under_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """仿真实文件：无标签一行 + 两条带标签 + 一行噪声。"""
    p = tmp_path / "Token.txt"
    p.write_text(f"{KEY_A}\n金融智能体：{KEY_B}\n这一行没有 key\n手机操作：sk-{'c' * 32}\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------


def test_mask_hides_middle_and_handles_empty(uk):
    assert uk.mask(KEY_A) == "sk-a***aaaa"
    assert uk.mask("") == "(空)"
    assert uk.mask("short") == "***"          # 太短一律全遮，别漏出去半个
    assert KEY_A not in uk.mask(KEY_A)


# ---------------------------------------------------------------------------
# 解析与挑选
# ---------------------------------------------------------------------------


def test_parse_entries_reads_labeled_and_bare_lines(uk, source):
    got = uk.parse_entries(source)
    assert [lab for lab, _ in got] == ["", "金融智能体", "手机操作"]
    assert [k for _, k in got][0] == KEY_A
    assert len(got) == 3                      # 没 key 的那行被忽略


def test_pick_by_label_and_by_line(uk, source):
    entries = uk.parse_entries(source)
    assert uk.pick(entries, label="金融智能体", line=0)[1] == KEY_B
    assert uk.pick(entries, label="", line=1)[1] == KEY_A


def test_pick_refuses_unknown_label_and_bad_line(uk, source):
    entries = uk.parse_entries(source)
    with pytest.raises(SystemExit) as e:
        uk.pick(entries, label="不存在", line=0)
    assert "金融智能体" in str(e.value)        # 拒绝时要把现有标签列出来，人才知道怎么改

    with pytest.raises(SystemExit):
        uk.pick(entries, label="", line=99)


def test_parse_entries_on_file_without_keys(uk, tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("什么都没有\n", encoding="utf-8")
    assert uk.parse_entries(p) == []


# ---------------------------------------------------------------------------
# main：写入行为与红线
# ---------------------------------------------------------------------------


def test_main_writes_secrets_without_leaking_plaintext(uk, source, tmp_path, capsys):
    target = tmp_path / ".secrets" / "llm.json"
    uk.SECRETS = target

    rc = uk.main(["--source", str(source), "--label", "金融智能体",
                  "--model", "deepseek-flash", "--base-url", "https://api.deepseek.com/v1"])
    out = capsys.readouterr().out

    assert rc == 0
    assert KEY_B not in out, "明文 key 绝不能出现在输出里"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"api_key": KEY_B, "base_url": "https://api.deepseek.com/v1",
                       "model": "deepseek-flash"}


def test_main_show_reports_masked_config(uk, tmp_path, capsys):
    target = tmp_path / "llm.json"
    target.write_text(json.dumps({"api_key": KEY_A, "model": "deepseek-flash"}),
                      encoding="utf-8")
    uk.SECRETS = target

    assert uk.main(["--show"]) == 0
    out = capsys.readouterr().out
    assert KEY_A not in out and "sk-a***aaaa" in out


def test_main_show_on_missing_file_is_not_an_error(uk, tmp_path, capsys):
    uk.SECRETS = tmp_path / "没有这个文件.json"
    assert uk.main(["--show"]) == 0
    assert "不存在" in capsys.readouterr().out


def test_main_fails_loudly_on_missing_source(uk, tmp_path, capsys):
    uk.SECRETS = tmp_path / "llm.json"
    rc = uk.main(["--source", str(tmp_path / "没有这个文件.txt")])
    assert rc == 2
    assert not (tmp_path / "llm.json").exists(), "失败时不许留下一份空配置"
