"""内容寻址的对象存储（原文层）。

一切大文本 / 二进制原文都落这里，按 sha256 寻址：

    blobs/<前两位>/<sha256>.bin

不变量：
- 同内容同 hash；
- 同 hash 只落盘一次（幂等，绝不重复写、绝不覆盖已存在内容）；
- 写入原子：先写**同目录**临时文件，再 ``os.replace`` 到目标名。
  同目录才能保证 replace 是同一文件系统上的原子重命名。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..contracts import sha256_bytes
from .paths import blobs_dir

_HEX = set("0123456789abcdef")


def _norm_sha(sha: str) -> str:
    """校验并归一化 sha256（小写十六进制，定长 64）。"""
    s = (sha or "").strip().lower()
    if len(s) != 64 or any(c not in _HEX for c in s):
        raise ValueError(f"非法 sha256：{sha!r}")
    return s


def path_for(sha: str) -> Path:
    """返回内容寻址路径：``blobs/<前两位>/<sha>.bin``。"""
    s = _norm_sha(sha)
    return blobs_dir() / s[:2] / f"{s}.bin"


def exists(sha: str) -> bool:
    """该 hash 是否已落盘。传入非法 hash 时返回 False（不抛异常）。"""
    try:
        return path_for(sha).is_file()
    except ValueError:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写字节：同目录临时文件 → ``os.replace``。失败时清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def put_bytes(data: bytes) -> str:
    """写入字节，返回 sha256 十六进制。同 hash 已存在则不重复落盘。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("put_bytes 只接受 bytes")
    raw = bytes(data)
    sha = sha256_bytes(raw)
    target = path_for(sha)
    if not target.is_file():
        _atomic_write_bytes(target, raw)
    return sha


def put_text(text: str) -> str:
    """写入文本（UTF-8 编码），返回 sha256 十六进制。"""
    if not isinstance(text, str):
        raise TypeError("put_text 只接受 str")
    return put_bytes(text.encode("utf-8"))


def get(sha: str) -> bytes:
    """按 hash 取回字节；不存在抛 ``KeyError``。"""
    target = path_for(sha)
    try:
        return target.read_bytes()
    except FileNotFoundError:
        raise KeyError(sha) from None


def get_text(sha: str) -> str:
    """按 hash 取回文本（UTF-8 解码）。"""
    return get(sha).decode("utf-8")
