"""t_search_official —— 从巨潮资讯（cninfo）取官方公告，产出 Document（BUILD_SPEC §7）。

两种模式：

* ``mode="network"``：向巨潮查询（可选下载 PDF 正文），产出 Document；
* ``mode="local"``：只查本地 manifest，**不触网**（离线测试与断网降级用）。

未命中返回空 ``documents``（不算失败），收据里 ``note`` 说明「未命中」。
只用标准库；网络访问集中在 ``fintrace_engine.sourcing.cninfo``。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..contracts import Acquisition, Document, MaterialFormat
from .registry import ToolError, ToolOutcome, ToolSpec, ctx_config, register

SPEC = ToolSpec(
    name="t_search_official",
    params={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "证券代码，如 002232"},
            "category": {"type": "string",
                         "description": "类别简称：ndbg/bndbg/yjdbg/sjdbg/sjbg"},
            "keyword": {"type": "string", "description": "标题关键词（更正公告用 '更正'）"},
            "se_date": {"type": "string", "description": "披露日期区间：2024-01-01~2025-12-31"},
            "limit": {"type": "integer", "description": "条数上限，默认 20"},
            "mode": {"type": "string", "enum": ["network", "local"],
                     "description": "network=向巨潮查询；local=只查本地 manifest（不触网）"},
            "cache_dir": {"type": "string", "description": "巨潮缓存目录"},
            "rate": {"type": "number", "description": "请求间隔秒（默认 0）"},
            "download": {"type": "boolean", "description": "是否下载 PDF 正文"},
            "overwrite": {"type": "boolean", "description": "下载时是否覆盖已有文件"},
        },
        "required": [],
    },
    receipt={"fields": ["documents", "count", "mode", "note"]},
    preconditions=[
        "至少给出 code / keyword / category 之一",
        "mode=network 需可达巨潮且有映射表缓存；mode=local 不触网",
    ],
    timeout_s=30,
)


def _safe_name(name: str, maxlen: int = 60) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name or "").strip("_")
    return (cleaned or "untitled")[:maxlen]


def _stable_id(code: str, title: str) -> str:
    """没有 announcement_id 时，用 code+title 的 sha256 前缀造稳定 id（不用 hash()）。"""
    return hashlib.sha256(f"{code}|{title}".encode("utf-8")).hexdigest()[:12]


def _doc_from_rec(rec: dict) -> Document:
    ann_id = str(rec.get("announcement_id") or rec.get("id") or "")
    code = str(rec.get("code") or "")
    title = str(rec.get("title") or "")
    doc_id = f"CNINFO-{ann_id}" if ann_id else f"CNINFO-{code}-{_stable_id(code, title)}"
    return Document(
        doc_id=doc_id,
        source_id=code,
        file_name=_safe_name(title) + ".PDF",
        source_type=str(rec.get("category") or ""),
        publication_date=str(rec.get("published_at") or ""),
        page_count=rec.get("pages"),
        acquisition=Acquisition.CNINFO.value,
        content_path=str(rec.get("path") or rec.get("url") or ""),
        content_hash=str(rec.get("sha256") or ""),
        material_format=MaterialFormat.PDF.value,
        retrieved_at=str(rec.get("fetched_at") or ""),
    )


def run(args: dict, ctx) -> ToolOutcome:
    code = str(args.get("code", "")).strip()
    category = str(args.get("category", "")).strip()
    keyword = str(args.get("keyword", "")).strip()
    se_date = str(args.get("se_date", "")).strip()
    mode = str(args.get("mode", "network")).strip() or "network"
    if mode not in ("network", "local"):
        raise ToolError("bad_args", "mode 只能是 network/local")
    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        raise ToolError("bad_args", "limit 必须是整数")
    if limit <= 0:
        raise ToolError("bad_args", "limit 必须为正")
    if not (code or keyword or category):
        raise ToolError("bad_args", "至少给出 code / keyword / category 之一")

    cfg = ctx_config(ctx)
    cache_dir = args.get("cache_dir") or cfg.get("sourcing_cache") or "data/sourced"

    from fintrace_engine.sourcing.cninfo import CninfoClient, search_local

    client = CninfoClient(cache_dir=cache_dir, rate=float(args.get("rate", 0.0) or 0.0))

    if mode == "local":
        manifest = client.load_manifest()
        hits = search_local(manifest, text=keyword, code=code, category=category)
        docs = [_doc_from_rec(h) for h in hits[:limit]]
        return ToolOutcome(result={
            "documents": [d.to_dict() for d in docs],
            "count": len(docs),
            "mode": "local",
            "note": "" if docs else "未命中本地清单",
        })

    anns = client.announcements(code=code or None, category=category, keyword=keyword,
                                se_date=se_date, limit=limit)
    download = bool(args.get("download"))
    docs = []
    for a in anns:
        rec = a.as_dict()
        rec["category"] = a.category
        if download:
            rec.update(client.download(a, overwrite=bool(args.get("overwrite"))))
        docs.append(_doc_from_rec(rec))
    return ToolOutcome(result={
        "documents": [d.to_dict() for d in docs],
        "count": len(docs),
        "mode": "network",
        "note": "" if docs else "未命中",
    })


register(SPEC, run)

__all__ = ["SPEC", "run"]
