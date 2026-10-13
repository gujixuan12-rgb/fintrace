"""外部材料快照（线索/网页材料）。

正文走内容寻址库（``blobs/``），元信息存 sidecar ``snapshots/<hash>.json``。

不变量（v2.2 §5.2 L3）：
- **一切时间判定用 ``first_published_at``（首次公开时间），绝不用 ``retrieved_at``（抓取时间）**；
- 没有 ``first_published_at`` 的快照一律判为「不可用」（``before_cutoff`` 返回 ``False``）。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from . import blobs
from .paths import snapshots_dir
from .runs import atomic_write_json

_HEX = set("0123456789abcdef")


def _norm_sha(sha: str) -> str:
    s = (sha or "").strip().lower()
    if len(s) != 64 or any(c not in _HEX for c in s):
        raise ValueError(f"非法 snapshot_hash：{sha!r}")
    return s


def _record_path(snapshot_hash: str) -> Path:
    return snapshots_dir() / f"{_norm_sha(snapshot_hash)}.json"


def _now_iso() -> str:
    """本地带时区的 ISO 时间戳（抓取时间）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_record(snapshot_hash: str) -> dict:
    path = _record_path(snapshot_hash)
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(rec: dict) -> dict:
    """record() 的返回视图（恰好契约里的五个字段）。"""
    return {
        "snapshot_hash": rec["snapshot_hash"],
        "retrieved_at": rec.get("retrieved_at", ""),
        "path": str(_record_path(rec["snapshot_hash"])),
        "first_published_at": rec.get("first_published_at", ""),
        "version": rec.get("version", ""),
    }


def record(url: str, text: str, *, meta: dict | None = None,
           first_published_at: str = "", version: str = "") -> dict:
    """登记一份快照，返回 ``{snapshot_hash, retrieved_at, path, first_published_at, version}``。

    ``snapshot_hash`` 是正文的 sha256（内容寻址）：相同正文得到相同 hash。
    同 hash 已有记录时不覆盖（原文留档），直接返回既有记录的元信息。
    """
    snapshot_hash = blobs.put_text(text)
    if _record_path(snapshot_hash).is_file():
        return _summary(_read_record(snapshot_hash))
    rec = {
        "snapshot_hash": snapshot_hash,
        "url": url,
        "retrieved_at": _now_iso(),
        "first_published_at": first_published_at,
        "version": version,
        "meta": dict(meta or {}),
    }
    atomic_write_json(_record_path(snapshot_hash), rec)
    return _summary(rec)


def load(snapshot_hash: str) -> dict:
    """取回快照：``{"text": 正文, "meta": {url, retrieved_at, first_published_at, version, ...}}``。

    hash 不存在抛 ``KeyError``。
    """
    path = _record_path(snapshot_hash)
    if not path.is_file():
        raise KeyError(snapshot_hash)
    rec = json.loads(path.read_text(encoding="utf-8"))
    meta = dict(rec.get("meta") or {})
    meta.update({
        "snapshot_hash": rec.get("snapshot_hash", snapshot_hash),
        "url": rec.get("url", ""),
        "retrieved_at": rec.get("retrieved_at", ""),
        "first_published_at": rec.get("first_published_at", ""),
        "version": rec.get("version", ""),
    })
    return {"text": blobs.get_text(snapshot_hash), "meta": meta}


def _first_published_at(snapshot: dict) -> str:
    """从快照 dict（record 的扁平返回 / load 的嵌套 meta）里取首曝时间。"""
    if not snapshot:
        return ""
    value = snapshot.get("first_published_at")
    if not value:
        value = (snapshot.get("meta") or {}).get("first_published_at")
    return (value or "").strip()


def _parse_date(value: str) -> date:
    """解析日期，接受 ``YYYY-MM-DD`` 或 ISO datetime（取日期部分）。"""
    s = (value or "").strip()
    if not s:
        raise ValueError("空时间")
    core = s.split("T")[0].split(" ")[0]
    return date.fromisoformat(core)


def before_cutoff(snapshot: dict, cutoff: str) -> bool:
    """该快照是否「在截止日之前已公开」。

    判定一律用 ``first_published_at``（首次公开时间），**不是** ``retrieved_at``（抓取时间）。
    因此「抓取时间早于 cutoff 但首曝晚于 cutoff」必须返回 ``False``。
    缺 ``first_published_at``（或时间非法）一律返回 ``False``（不可用）。
    """
    first_published = _first_published_at(snapshot)
    if not first_published:
        return False
    try:
        return _parse_date(first_published) <= _parse_date(cutoff)
    except ValueError:
        return False


__all__ = ["record", "load", "before_cutoff"]
