"""store/snapshots.py 测试：首曝时间判定（L3 关键反例）。"""
from __future__ import annotations

import json

import pytest

from fintrace_agent.store import blobs, snapshots


def test_record_returns_contract_fields(fintrace_home):
    out = snapshots.record("https://x.example/a", "正文", first_published_at="2023-05-01",
                           version="v1")
    assert set(out) == {"snapshot_hash", "retrieved_at", "path", "first_published_at", "version"}
    assert out["first_published_at"] == "2023-05-01"
    assert out["version"] == "v1"
    assert out["retrieved_at"]                     # 抓取时间非空
    assert out["snapshot_hash"] == blobs.put_text("正文")
    # sidecar 真的落盘了
    assert (fintrace_home / "snapshots" / f"{out['snapshot_hash']}.json").is_file()


def test_load_roundtrip(fintrace_home):
    snap = snapshots.record("https://x.example/b", "中文正文 · 1,000 万元",
                            first_published_at="2023-05-01", meta={"k": "v"})
    loaded = snapshots.load(snap["snapshot_hash"])
    assert loaded["text"] == "中文正文 · 1,000 万元"
    assert loaded["meta"]["url"] == "https://x.example/b"
    assert loaded["meta"]["first_published_at"] == "2023-05-01"
    assert loaded["meta"]["k"] == "v"       # 用户自定义 meta 原样保留


def test_same_text_same_hash_and_not_overwritten(fintrace_home):
    a = snapshots.record("https://x.example/c", "同样的正文",
                         first_published_at="2023-01-01", version="v1")
    # 同正文再登记一次（首曝时间不同）：hash 相同，且不覆盖既有记录
    b = snapshots.record("https://x.example/c", "同样的正文",
                         first_published_at="2023-09-09", version="v2")
    assert a["snapshot_hash"] == b["snapshot_hash"]
    assert b["first_published_at"] == "2023-01-01"     # 保留首次登记值
    assert b["version"] == "v1"

    sidecar = fintrace_home / "snapshots" / f"{a['snapshot_hash']}.json"
    rec = json.loads(sidecar.read_text(encoding="utf-8"))
    assert rec["first_published_at"] == "2023-01-01"


def test_load_missing_raises_keyerror():
    with pytest.raises(KeyError):
        snapshots.load("f" * 64)


def test_before_cutoff_true_when_published_early():
    snap = {"first_published_at": "2023-01-01", "retrieved_at": "2026-10-10"}
    assert snapshots.before_cutoff(snap, "2023-06-01") is True


def test_before_cutoff_equal_day_is_true():
    snap = {"first_published_at": "2023-06-01"}
    assert snapshots.before_cutoff(snap, "2023-06-01") is True


def test_before_cutoff_false_when_published_late():
    snap = {"first_published_at": "2023-12-31", "retrieved_at": "2023-01-01"}
    assert snapshots.before_cutoff(snap, "2023-06-01") is False


def test_before_cutoff_uses_first_published_not_retrieved():
    """L3 关键反例：抓取时间早于 cutoff，但首曝晚于 cutoff → 必须 False。

    如果实现误用 retrieved_at，这里会得到 True。
    """
    snap = {
        "snapshot_hash": "a" * 64,
        "retrieved_at": "2023-05-01",        # 抓取时间 < cutoff
        "first_published_at": "2023-12-01",  # 首曝时间 > cutoff
        "version": "",
    }
    assert snapshots.before_cutoff(snap, "2023-06-01") is False


def test_before_cutoff_false_without_first_published():
    """缺 first_published_at 一律判不可用。"""
    assert snapshots.before_cutoff({"retrieved_at": "2023-01-01"}, "2023-06-01") is False
    assert snapshots.before_cutoff({"first_published_at": ""}, "2023-06-01") is False
    assert snapshots.before_cutoff({}, "2023-06-01") is False


def test_before_cutoff_accepts_load_result():
    """load() 返回把首曝时间放在 meta 里，before_cutoff 也必须能读。"""
    loaded = {"text": "x", "meta": {"first_published_at": "2023-12-01",
                                    "retrieved_at": "2023-01-01"}}
    assert snapshots.before_cutoff(loaded, "2023-06-01") is False

    loaded_early = {"text": "x", "meta": {"first_published_at": "2023-01-01"}}
    assert snapshots.before_cutoff(loaded_early, "2023-06-01") is True


def test_before_cutoff_invalid_date_is_false():
    assert snapshots.before_cutoff({"first_published_at": "not-a-date"}, "2023-06-01") is False
    assert snapshots.before_cutoff({"first_published_at": "2023-01-01"}, "坏cutoff") is False


def test_before_cutoff_accepts_iso_datetime():
    snap = {"first_published_at": "2023-01-01T09:30:00+08:00"}
    assert snapshots.before_cutoff(snap, "2023-06-01") is True


def test_record_then_before_cutoff_flat(fintrace_home):
    snap = snapshots.record("https://x.example/d", "线索", first_published_at="2023-03-03")
    assert snapshots.before_cutoff(snap, "2023-06-01") is True
    assert snapshots.before_cutoff(snap, "2023-01-01") is False
