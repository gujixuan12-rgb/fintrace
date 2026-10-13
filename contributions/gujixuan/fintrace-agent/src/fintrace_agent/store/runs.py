"""运行目录与原子写盘。

运行目录布局（v2.2 §3.5）：

    runs/<run_id>/state.json
    runs/<run_id>/findings.json
    runs/<run_id>/evidence.jsonl
    runs/<run_id>/decisions.jsonl
    runs/<run_id>/rejections.jsonl
    runs/<run_id>/report.md
    runs/<run_id>/snapshots/
    runs/<run_id>/tools/

本模块只负责：运行号生成、目录创建、JSON 的原子读写、JSONL 的只追加与容错读回。
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path

from .paths import runs_dir

#: 运行目录里固定存在的子目录（create=True 时预建）
RUN_SUBDIRS: tuple[str, ...] = ("snapshots", "tools")


def new_run_id(case_id: str = "") -> str:
    """生成运行号，形如 ``FT-20261010-a1b2c3d4``（本地日期 + 8 位十六进制）。

    ``case_id`` 为 API 兼容参数，当前不拼进运行号（格式是契约，不可自造）。
    """
    day = datetime.now().strftime("%Y%m%d")
    return f"FT-{day}-{secrets.token_hex(4)}"


def run_dir(run_id: str, create: bool = False) -> Path:
    """返回 ``runs/<run_id>/``；``create=True`` 时创建目录及固定子目录。"""
    rid = (run_id or "").strip()
    if not rid or any(sep in rid for sep in ("/", "\\")) or ".." in rid:
        raise ValueError(f"非法 run_id：{run_id!r}")
    path = runs_dir() / rid
    if create:
        path.mkdir(parents=True, exist_ok=True)
        for sub in RUN_SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
    return path


def atomic_write_json(path: Path, obj) -> None:
    """原子写 JSON：同目录临时文件 → ``os.replace``。写完必可读，绝不留半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict:
    """读 JSON；文件不存在或为空时返回 ``{}``。"""
    path = Path(path)
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


def append_jsonl(path: Path, obj) -> None:
    """向 ``.jsonl`` 追加一行 JSON（只追加；逐行 fsync，崩溃最多丢半行）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    """读回 ``.jsonl``。

    崩溃造成的半行（JSON 解析失败）会被忽略——这正好对应「只追加 + 行内原子」
    的语义：一行要么完整落盘，要么整行丢弃。
    """
    path = Path(path)
    if not path.is_file():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return out
