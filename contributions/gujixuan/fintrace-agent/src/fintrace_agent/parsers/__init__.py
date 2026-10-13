"""parsers/ —— 材料格式探测、docx 抽取、声明式抽取规格、Span 桥接（模块 D，v2.2 §5.3、§5.4）。

只用标准库；读 PDF 才允许 pymupdf（延迟 import，缺了就降级返回空）。
模块之间只经 ``fintrace_agent.contracts`` 的数据类通信。
"""
from __future__ import annotations

from .bridge import decimals_of, text_to_spans
from .docx_extract import extract_spans
from .format_probe import probe
from .spec_extract import load_spec, run_spec, validate_spec

__all__ = [
    "probe",
    "extract_spans",
    "validate_spec",
    "run_spec",
    "load_spec",
    "text_to_spans",
    "decimals_of",
]
