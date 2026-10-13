"""tests/parsers 公共夹具：现场用 zipfile 造一个最小 docx（不依赖任何外部文件）。

硬约束：所有临时文件写 tmp_path，绝不污染项目目录。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

#: 最小 docx 的段落 + 表格：两段正文 + 一张两行表格（含真实数值，便于测 decimals）
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    # 第一段：单 run
    "<w:p><w:r><w:t>启明信息2023年年度营业收入为408,548.46万元。</w:t></w:r></w:p>"
    # 第二段：两个 run（考验拼 run）
    "<w:p><w:r><w:t>预计</w:t></w:r><w:r><w:t>2024年营业收入增长</w:t></w:r></w:p>"
    # 一张两行表格
    "<w:tbl>"
    "<w:tr>"
    "<w:tc><w:p><w:r><w:t>现金分红金额（含税）</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>408,548.46</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>元</w:t></w:r></w:p></w:tc>"
    "</w:tr>"
    "<w:tr>"
    "<w:tc><w:p><w:r><w:t>每股收益</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>0.155</w:t></w:r></w:p></w:tc>"
    "</w:tr>"
    "</w:tbl>"
    "</w:body>"
    "</w:document>"
)


def build_docx(path: Path) -> Path:
    """把最小 docx 写进 ``path`` 并返回它。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("word/document.xml", _DOCUMENT_XML)
    return path


@pytest.fixture()
def sample_docx(tmp_path: Path) -> Path:
    return build_docx(tmp_path / "研报草稿.docx")
