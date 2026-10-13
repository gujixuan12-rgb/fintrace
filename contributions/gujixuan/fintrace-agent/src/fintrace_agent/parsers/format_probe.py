"""材料格式探测（v2.2 §5.3）：按魔数判格式，认不出就报 ``unknown``。

纪律：**探测不出就报「不认识」，不许当成空材料**（沿用「认不出就留空、不猜」的口径）。
只用标准库；不联网、不装依赖。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..contracts import MaterialFormat

#: 各格式的魔数（OLE 头是 .doc / .xls 共用的复合文档头）
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: docx 判定必须看到的容器内条目
DOCX_PART = "word/document.xml"

#: 文本探测只嗅探头部这些字节（够判编码，又不必读整份大文件）
_SNIFF = 65536

#: 文本编码试探顺序（gb18030 是 GBK/GB2312 的超集，覆盖中文文本）
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


def _hex(head: bytes) -> str:
    """头部前 8 字节的十六进制大写，作为「认得但没归类」时的签名。"""
    return head[:8].hex().upper()


def _result(fmt: MaterialFormat, why: str, signature: str) -> dict:
    return {"format": fmt.value, "why": why, "signature": signature}


def probe(path: str | Path) -> dict:
    """探测一份材料的格式。

    返回 ``{"format": MaterialFormat 值, "why": str, "signature": str}``。
    判不出返回 ``unknown``（**不是**空材料）。
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return _result(MaterialFormat.UNKNOWN, f"文件不存在或不是普通文件：{p}", "")

    try:
        with p.open("rb") as fh:
            head = fh.read(_SNIFF)
    except OSError as exc:  # 权限/占用等
        return _result(MaterialFormat.UNKNOWN, f"读取失败：{exc}", "")

    if head.startswith(PDF_MAGIC):
        return _result(MaterialFormat.PDF, "头部匹配 %PDF-", "%PDF-")

    if head.startswith(ZIP_MAGIC):
        # zip 容器本身不是 docx：必须见到 word/document.xml 才算
        try:
            with zipfile.ZipFile(p) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, OSError) as exc:
            return _result(MaterialFormat.UNKNOWN, f"zip 容器损坏：{exc}", _hex(head))
        if DOCX_PART in names:
            return _result(MaterialFormat.DOCX, "zip 容器且含 word/document.xml",
                           f"PK\\x03\\x04 + {DOCX_PART}")
        return _result(MaterialFormat.UNKNOWN, "zip 容器但无 word/document.xml", "PK\\x03\\x04")

    if head.startswith(OLE_MAGIC):
        return _result(MaterialFormat.DOC, "头部匹配 OLE 复合文档头 D0 CF 11 E0", "D0 CF 11 E0")

    # 文本：无 NUL 字节且能用某编码完整解码
    if b"\x00" not in head:
        for enc in _TEXT_ENCODINGS:
            try:
                head.decode(enc)
            except UnicodeDecodeError:
                continue
            return _result(MaterialFormat.TEXT, f"纯文本，可用 {enc} 解码", f"text/{enc}")

    return _result(MaterialFormat.UNKNOWN, "魔数不认识且非可解码文本", _hex(head))


__all__ = ["probe"]
