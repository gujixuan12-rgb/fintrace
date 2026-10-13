"""store/blobs.py 测试：内容寻址、幂等、原子写。"""
from __future__ import annotations

import hashlib
import os

import pytest

from fintrace_agent.store import blobs


def test_put_bytes_same_content_same_hash():
    assert blobs.put_bytes(b"hello") == blobs.put_bytes(b"hello")


def test_put_bytes_matches_sha256():
    assert blobs.put_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_put_and_get_roundtrip():
    sha = blobs.put_bytes(b"\x00\x01\x02binary")
    assert blobs.get(sha) == b"\x00\x01\x02binary"


def test_put_and_get_text_roundtrip():
    sha = blobs.put_text("中文原文 · 4,085,484.55")
    assert blobs.get_text(sha) == "中文原文 · 4,085,484.55"
    # 文本 hash 与 UTF-8 字节 hash 一致
    assert sha == hashlib.sha256("中文原文 · 4,085,484.55".encode("utf-8")).hexdigest()


def test_path_for_layout():
    sha = blobs.put_bytes(b"x")
    path = blobs.path_for(sha)
    assert path.name == f"{sha}.bin"
    assert path.parent.name == sha[:2]
    assert path.parent.parent.name == "blobs"


def test_exists():
    sha = blobs.put_bytes(b"exists?")
    assert blobs.exists(sha) is True
    assert blobs.exists("0" * 64) is False


def test_get_missing_raises_keyerror():
    with pytest.raises(KeyError):
        blobs.get("f" * 64)


def test_idempotent_does_not_rewrite(monkeypatch):
    sha = blobs.put_bytes(b"once")
    # 第二次同内容写入不得再触发落盘（os.replace 不应该被调用）
    def boom(*_a, **_k):  # pragma: no cover - 只应在被错误调用时触发
        raise AssertionError("同 hash 不应重复落盘")

    monkeypatch.setattr(os, "replace", boom)
    assert blobs.put_bytes(b"once") == sha


def test_put_is_atomic_tempfile_then_replace(monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    sha = blobs.put_bytes(b"atomic")

    assert calls, "put_bytes 必须经 os.replace 落盘（临时文件 + 原子重命名）"
    src, dst = calls[-1]
    assert os.path.basename(dst) == f"{sha}.bin"
    # 同目录，保证 replace 是同一文件系统上的原子操作
    assert os.path.dirname(src) == os.path.dirname(dst)
    assert os.path.basename(src).startswith(".tmp-")
    # 正式文件写入后不存在残留临时文件
    leftovers = [n for n in os.listdir(os.path.dirname(dst)) if n.startswith(".tmp-")]
    assert leftovers == []


def test_put_bytes_rejects_non_bytes():
    with pytest.raises(TypeError):
        blobs.put_bytes("not bytes")  # type: ignore[arg-type]
