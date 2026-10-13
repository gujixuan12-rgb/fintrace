"""t_parse —— 材料解析：探测格式 → 分派抽取算子 → list[Span]（BUILD_SPEC §7）。

分派规则：

* ``docx``  → ``parsers.docx_extract.extract_spans``（段落 / 表格行）
* ``text``  → ``parsers.bridge.text_to_spans``
* ``pdf``   → ``fintrace_engine.parsers.pdf_pages`` 逐页取文 → ``text_to_spans``
* 未知格式  → ``unknown_material_format``（认不出就报错，不当成空材料）

若 ``args`` 里带了 ``spec``，则改走声明式抽取（``run_spec``）；规格非法一律拒绝。
只用标准库；只有读真实 PDF 才延迟导入 pymupdf。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..parsers import extract_spans, probe, run_spec, text_to_spans
from ..parsers.bridge import decimals_of  # noqa: F401  (保持一致入口，供下游引用)
from .registry import ToolError, ToolOutcome, ToolSpec, register

SPEC = ToolSpec(
    name="t_parse",
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "材料文件路径（必填）"},
            "doc_id": {"type": "string", "description": "材料 id"},
            "spec": {"type": "object", "description": "可选的声明式抽取规格"},
            "pages": {"type": "array",
                       "description": "配合 spec 用的逐页文本 [{'page','text'}]；"
                                      "PDF 下可写页码列表 [7,8] 或 \"7-9\" 只解析这些页"},
            "start_span": {"type": "integer", "description": "起始 span 序号"},
        },
        "required": ["path"],
    },
    receipt={"fields": ["format", "extractor", "spans", "probe", "count"]},
    preconditions=["path 指向存在的文件", "spec 存在时必须通过 parsers.spec_extract.validate_spec"],
    timeout_s=30,
)

_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


def _page_filter(raw) -> set | None:
    """``pages`` 可以写 ``[7, 8]`` 或 ``"7-9"``；返回要保留的页码集合（None = 全要）。

    为什么需要：整本年报动辄几百页、上千条切片，一次全灌进来既慢又把上下文吃光。
    Agent 该做的是「先看草稿主张涉及哪些科目，再定向取那几页」。
    """
    if raw is None or raw == "":
        return None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    want: set = set()
    for it in items:
        if isinstance(it, dict):
            continue
        s = str(it).strip()
        m = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b:
                want.update(range(a, b + 1))
            continue
        if s.isdigit():
            want.add(int(s))
    return want or None


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in _TEXT_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(args: dict, ctx) -> ToolOutcome:
    raw_path = args.get("path")
    if not raw_path or not str(raw_path).strip():
        raise ToolError("bad_args", "缺少 path")
    path = Path(str(raw_path))
    if not path.exists() or not path.is_file():
        raise ToolError("not_found", f"材料不存在：{path}")

    doc_id = str(args.get("doc_id") or "")
    probe_result = probe(path)

    spec = args.get("spec")
    if isinstance(spec, dict):
        pages = args.get("pages")
        if not isinstance(pages, list) or not pages:
            # 没有现成 pages，就用内置路径先把文本读成 pages 再交给 spec 解释
            fmt = probe_result["format"]
            if fmt == "text":
                pages = [{"page": 1, "text": _read_text(path), "doc_id": doc_id}]
            elif fmt == "docx":
                spans = extract_spans(path, doc_id,
                                      start_span=int(args.get("start_span", 0) or 0))
                pages = [{"page": None, "text": s.quoted_text, "doc_id": doc_id} for s in spans]
            elif fmt == "pdf":
                pages = _pdf_pages(path, doc_id)
            else:
                raise ToolError("unknown_material_format",
                                f"探测结果 {fmt}：{probe_result['why']}")
        try:
            spans = run_spec(spec, pages)
        except ValueError as exc:  # run_spec 校验失败抛 ValueError
            raise ToolError("invalid_spec", str(exc)) from exc
        return ToolOutcome(result={
            "format": probe_result["format"],
            "extractor": "spec",
            "probe": probe_result,
            "spans": [s.to_dict() for s in spans],
            "count": len(spans),
        })

    fmt = probe_result["format"]
    start_span = int(args.get("start_span", 0) or 0)

    if fmt == "docx":
        spans = extract_spans(path, doc_id, start_span=start_span)
        extractor = "docx"
    elif fmt == "text":
        pages = [{"page": 1, "text": _read_text(path)}]
        spans = text_to_spans(pages, doc_id, extractor="builtin")
        extractor = "builtin"
    elif fmt == "pdf":
        pages = _pdf_pages(path, doc_id)
        want = _page_filter(args.get("pages"))          # 按页分批：只解析指定页
        if want:
            pages = [p for p in pages if p.get("page") in want]
        spans = text_to_spans(pages, doc_id, extractor="builtin")
        extractor = "builtin"
    else:
        raise ToolError("unknown_material_format",
                        f"探测结果 {fmt}：{probe_result['why']}")

    return ToolOutcome(result={
        "format": fmt,
        "extractor": extractor,
        "probe": probe_result,
        "spans": [s.to_dict() for s in spans],
        "count": len(spans),
    })


def _pdf_pages(path: Path, doc_id: str) -> list[dict]:
    """逐页文本（延迟导入 pymupdf；缺依赖时由上层捕获成 ok=False）。"""
    from fintrace_engine.parsers import pdf_pages as _pp

    return [{"page": pg.page, "text": pg.raw, "doc_id": doc_id} for pg in _pp.load(path)]


register(SPEC, run)

__all__ = ["SPEC", "run"]
