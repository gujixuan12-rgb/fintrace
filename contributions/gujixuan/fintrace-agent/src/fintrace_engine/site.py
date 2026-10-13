"""本机演示站点：静态落地页 + 只读 JSON API。

用途：把真实的核查链路搬到浏览器里演示。页面上的每一条结论、每一张红框截图、
每一份 PDF 都由本模块现算现给，不做预录数据。

工程约束：
- 只监听 127.0.0.1，不对外网开放（与项目红线一致）；
- 只用标准库 http.server；pymupdf 只在出截图/PDF 时才用到（仍是可选依赖）；
- 判定环节一律走规则路径，大模型不参与判定；草稿拆解可选走大模型（样本里的 split_mode：
  auto/rules/llm），但拆出来的主张会连同草稿指纹落盘复用——模型只跑一次，之后每次运行都用
  同一份主张，所以同一份草稿重跑结果逐字相同；
- 真值只用于展示对照，不参与判定。

路由：
    GET  /                              落地页
    GET  /assets/*                      静态资源
    GET  /api/samples                   样本清单（含材料是否就位）
    GET  /api/run?case_id=...&resplit=1 跑核查，返回逐条结论（JSON）；resplit=1 强制重拆草稿
    GET  /api/shot/<case>/<claim>/<i>.png   原文出错处红框截图（默认缩略图，?full=1 取原尺寸）
    GET  /api/report/<case>.pdf         逐条核查报告 PDF
    GET  /api/lookup?q=...              按公司名或代码找候选公司（走本地映射表）
    GET  /api/reports?code=...&kind=... 列这家公司的报告/更正公告，供人挑一份
    POST /api/detect                    上传材料后先认一遍公司/代码/报告期/披露日（不落盘）
    POST /api/import                    导入自有材料（上传的 + 从巨潮挑的），生成自建样本
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import os
import re
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import draft_review
from .parsers import report_identity
from .reporting import pdf_report
from .sourcing.cninfo import Announcement, CninfoClient, SourcingError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8921

ROOT = pdf_report.ROOT
WEB_DIR = ROOT / "web"
CASES_DIR = ROOT / "data" / "annotations" / "cases"
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "out" / "_site"
# 网页端导入的自有材料：与策展样本分开存，撤掉一个目录就干净了
IMPORTS_DIR = ROOT / "data" / "imports"
IMPORT_CASES_DIR = IMPORTS_DIR / "cases"

#: 导入材料允许的来源类型（与 models.SOURCE_TYPES 一致，这里只列界面上给的几个）
IMPORT_ROLES = {
    "original_annual_report": "定期报告（年报/半年报/季报）",
    "correction_announcement": "更正公告",
    "announcement": "公司公告",
    "research_report": "券商/第三方研报",
    "audit_report": "审计报告",
    "inquiry_letter_reply": "监管问询函及回复",
    "original_interim_report": "临时公告",
}
MAX_MATERIAL_BYTES = 60 * 1024 * 1024
MAX_IMPORT_BYTES = 200 * 1024 * 1024
MAX_DRAFT_BYTES = 2 * 1024 * 1024
#: 单输入框对话一轮请求体的上限（草稿正文一般几十 KB，留足富余）
CHAT_MAX_BYTES = 24 * 1024 * 1024

# 内存缓存：同一次进程内不重复跑链路。
# 三类产物（结果 / 截图 / 报告 PDF）都以「输入指纹」为键的一部分：改了截止日、草稿正文或
# 材料原件，指纹就变，旧产物不会被当成新的返回。只按样本号缓存吃过一次教训 ——
# 改截止日之后连案例文件都不再重读，旧结果被原样返回（P1-11 探针实测，见
# tools/check_guardrails.py 第六节）。
_RUN_CACHE: dict[str, tuple[str, dict]] = {}
_SHOT_CACHE: dict[tuple[str, str, str, int, bool], bytes] = {}
SHOT_CACHE_MAX = 80  # 每张几十 KB～几百 KB，给个上限免得长时间演示把内存撑起来


class SiteError(RuntimeError):
    """站点侧的可预期错误（转成 JSON 错误响应，不抛栈）。"""


# ---------------------------------------------------------------------------
# 样本与材料
# ---------------------------------------------------------------------------


def _case_files() -> list[Path]:
    """策展样本 + 网页端导入的自建样本（后者带 origin=imported，界面会标出来）。"""
    return sorted(CASES_DIR.glob("*.json")) + sorted(IMPORT_CASES_DIR.glob("*.json"))


def load_case(case_id: str) -> dict:
    for path in _case_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("case_id") == case_id:
            return data
    raise SiteError(f"找不到样本 {case_id}")


def _material_dir(case: dict) -> Path:
    """材料目录按股票代码分（data/raw/<code>/），不能按文件名找——同名文件跨公司重复。"""
    return RAW_DIR / str(case.get("stock_code") or "")


def material_paths(case: dict) -> list[tuple[str, str, Path]]:
    """[(file_name, source_type, 绝对路径)]，顺序同案例文件。

    优先用案例自己写的 path（导入的自建样本材料在 data/imports 下，不在 data/raw）；
    没写才退回 data/raw/<股票代码>/<文件名>。
    """
    base = _material_dir(case)
    out = []
    for d in case.get("documents") or []:
        name = d.get("file_name")
        if not name:
            continue
        raw = d.get("path")
        if raw:
            p = Path(raw)
            path = p if p.is_absolute() else ROOT / p
        else:
            path = base / name
        out.append((name, d.get("source_type") or "", path))
    return out


def _gold_summary(case: dict) -> dict:
    """真值统计（只用于页面上的对照展示；真值绝不参与判定）。

    真值文件里的条号是人工编的（QMD-001 / TXX-001），与引擎自己拆出来的条号
    （DRAFT-C001…）不是一套，所以这里只取它自己的 summary 与逐条 expected_verdict，
    不按条号做映射。
    """
    rel = case.get("expected")
    if not rel:
        return {}
    path = ROOT / rel
    if not path.exists():
        return {}
    try:
        gold = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    claims = gold.get("claims") or []
    labels: dict[str, str] = {}
    for c in claims:
        cid = c.get("claim_id")
        lab = c.get("expected_verdict") or c.get("expected_label")
        if cid and lab:
            labels[cid] = lab
    raw = gold.get("summary") or {}
    counts = {
        "SUPPORTED": int(raw.get("expected_supported") or 0),
        "CONTRADICTED": int(raw.get("expected_contradicted") or 0),
        "INSUFFICIENT": int(raw.get("expected_insufficient") or 0),
    }
    if not any(counts.values()):  # 没有 summary 就退回逐条数
        for lab in labels.values():
            if lab in counts:
                counts[lab] += 1
    return {
        "claims": int(raw.get("total_claims") or len(claims)),
        "counts": counts,
        "error_types": raw.get("error_type_counts") or {},
        "interpretation_limit": gold.get("interpretation_limit"),
        "sample_note": gold.get("sample_note"),
        "labels": labels,
    }


def list_samples() -> list[dict]:
    out = []
    for path in _case_files():
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        docs = [
            {"file_name": name, "source_type": st, "present": p.exists()}
            for name, st, p in material_paths(case)
        ]
        out.append({
            "case_id": case.get("case_id"),
            "company": case.get("company"),
            "stock_code": case.get("stock_code"),
            "period": case.get("period"),
            "prediction_cutoff_date": case.get("prediction_cutoff_date"),
            "cutoff_rationale": case.get("cutoff_rationale"),
            "notes": case.get("notes"),
            "documents": docs,
            "ready": bool(docs) and all(d["present"] for d in docs),
            "origin": case.get("origin") or "curated",
            "gold": _gold_summary(case),
        })
    return out


# ---------------------------------------------------------------------------
# 导入自有材料（网页端上传）
# ---------------------------------------------------------------------------


def _safe_name(name: str, *, default: str) -> str:
    """只留文件名本体，去掉路径与怪字符——这里收的是外部输入。"""
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(". ")
    return base or default


def forget_case(case_id: str) -> None:
    """丢掉某个样本的缓存。

    重新导入会用回同一个样本号（公司+报告期决定），如果不清缓存，第二次跑出来的
    还是上一份材料的裁决和截图——截图会指向另一份原件，属于证据造假，必须避免。
    """
    _RUN_CACHE.pop(case_id, None)
    for key in [k for k in _SHOT_CACHE if k[0] == case_id]:
        _SHOT_CACHE.pop(key, None)
    # 拆解缓存也要清：换了草稿还留着上一份的主张，等于拿旧主张核新材料
    try:
        (IMPORTS_DIR / case_id / CLAIMS_FILE).unlink()
    except OSError:
        pass


def _rel_path(path: Path) -> str:
    """尽量写成相对 ROOT 的路径（换机器也能跑）；真不在 ROOT 下就写绝对路径。"""
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _new_case_id(base: str) -> str:
    """生成不撞车的样本号。"""
    taken = {p.stem for p in _case_files()}
    base = re.sub(r"[^A-Za-z0-9_-]", "-", base.upper()).strip("-") or "IMP-USER"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _valid_date(raw: str) -> str:
    try:
        return date.fromisoformat((raw or "").strip()).isoformat()
    except ValueError as exc:
        raise SiteError(f"资料截止日要写成 2024-04-22 这种格式，收到的是 {raw!r}") from exc


def _material_date(raw: str, label: str) -> str:
    """材料自己的披露日：格式不对就报错点名是哪一份，别甩成「资料截止日」的错。"""
    try:
        return date.fromisoformat((raw or "").strip()).isoformat()
    except ValueError as exc:
        raise SiteError(f"材料「{label}」的披露日要写成 2024-04-22 这种格式，收到的是 {raw!r}") from exc


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str, bytes]]:
    """解 multipart/form-data → [(字段名, 文件名, 字节)]，文件名空串代表普通字段。

    Python 3.13 起标准库已无 cgi 模块，这里用 email 包解；不引第三方依赖。
    """
    from email.parser import BytesParser
    from email.policy import HTTP

    if "multipart/form-data" not in (content_type or "").lower():
        raise SiteError("导入要用 multipart/form-data 提交")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=HTTP).parsebytes(head + body)
    if not msg.is_multipart():
        raise SiteError("导入请求里没有表单内容")
    out: list[tuple[str, str, bytes]] = []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        out.append((str(name), part.get_filename() or "", part.get_payload(decode=True) or b""))
    return out


# ---------------------------------------------------------------------------
# 从巨潮现取材料：按公司名找 → 列报告 → 挑一份下回来
# ---------------------------------------------------------------------------

SOURCED_DIR = ROOT / "data" / "sourced"
REPORT_KINDS = {"annual": "年度报告", "halfyear": "半年度报告", "quarterly": "季度报告",
                "correction": "更正公告"}
#: kind → 巨潮公告分类。年报 ndbg / 半年报 bndbg；季报在巨潮分「一季报 / 三季报」两类，
#: 两个都要查（任务里写 2026Q1 时只查一类会什么都拿不到）。更正公告按关键词查，没有分类。
_KIND_CATEGORY = {"annual": ["ndbg"], "halfyear": ["bndbg"], "quarterly": ["yjdbg", "sjdbg"]}

_CNINFO: CninfoClient | None = None
#: announcement_id → (公告记录, 记下的时刻)。下载时只认这里登记过的号，
#: 前端传来的地址一律不采信（否则本机服务就成了任人指哪打哪的下载器）。
_ANN_CACHE: dict[str, tuple[Announcement, float]] = {}
_ANN_CACHE_TTL = 30 * 60.0


def _cninfo() -> CninfoClient:
    """取数客户端（进程内单例）。缓存目录与 `cli fetch` 共用，下过的不会再下一遍。"""
    global _CNINFO
    if _CNINFO is None:
        _CNINFO = CninfoClient(cache_dir=SOURCED_DIR, rate=0.6,
                               verbose=bool(os.environ.get("FINTRACE_SITE_LOG")))
    return _CNINFO


def lookup_companies(q: str, limit: int = 20) -> list[dict]:
    """按公司名或股票代码找候选公司。查的是本地映射表（全市场 6259 家），离线也能查。"""
    q = (q or "").strip()
    if len(q) < 2:
        raise SiteError("至少输入两个字，或者 3 位以上的股票代码")
    try:
        market = _cninfo().stock_map()
    except SourcingError as exc:
        raise SiteError(f"取股票映射表失败：{exc}") from exc
    if q.isdigit():
        hits = ([c for c in market if c == q] + [c for c in market if c.startswith(q) and c != q])
    else:
        hits = [c for c, m in market.items() if q in (m.get("name") or "")]
        # 名字短的排前面（「天下秀」优于「天下秀某某」），同长按代码
        hits.sort(key=lambda c: (len(market[c]["name"] or ""), c))
    return [{"code": c, "name": market[c]["name"], "orgId": market[c]["orgId"]} for c in hits[:limit]]


def list_reports(code: str, kind: str = "annual", limit: int = 40) -> list[dict]:
    """列这家公司的报告，供人挑。返回项带 `detected`：挑完不用下载就能先把表填上。"""
    code = re.sub(r"\D", "", code or "")[:6]
    if len(code) != 6:
        raise SiteError(f"股票代码要 6 位数字，收到的是 {code!r}")
    if kind not in REPORT_KINDS:
        raise SiteError(f"报告类型只支持 {'、'.join(REPORT_KINDS)}（收到 {kind!r}）")
    client = _cninfo()
    try:
        if kind == "correction":
            anns = client.announcements(code=code, keyword="更正", limit=limit)
        else:
            anns = []
            for cat in _KIND_CATEGORY[kind]:
                anns.extend(client.announcements(code=code, category=cat, limit=limit))
    except SourcingError as exc:
        raise SiteError(f"查公告失败：{exc}") from exc

    now = time.monotonic()
    out = []
    for a in anns:
        _ANN_CACHE[a.announcement_id] = (a, now)
        out.append({
            "announcement_id": a.announcement_id,
            "title": a.title,
            "published_at": a.published_at,
            "size_kb": a.size_kb,
            # 只在标题结尾出现「摘要」时才算摘要：更正公告的标题里会引用
            # 《2023年年度报告摘要》，按包含判断会把更正公告错标成摘要
            "is_summary": bool(re.search(r"摘要\s*(?:（[^）]*）)?$", a.title)),
            "detected": report_identity.from_index(a.title, a.code, a.name, a.published_at),
        })
    out.sort(key=lambda r: r["published_at"], reverse=True)
    return out


def _default_role(title: str) -> str:
    """公告标题 → 导入用的来源类型；对不上就按普通公告算。"""
    kind = report_identity.classify(title)
    return kind if kind in IMPORT_ROLES else "announcement"


def fetch_picked(picked: list[dict]) -> list[tuple[str, str, str, bytes]]:
    """把挑好的公告下回来 → [(文件名, 来源类型, 披露日, 字节)]。

    只认 `list_reports` 登记过的公告号：客户端说「我要这个编号」可以，说「你去下这个网址」
    不行。下载落 `data/sourced/<代码>/`（与 cli fetch 同一份缓存），再把字节交给导入流程。
    """
    out: list[tuple[str, str, str, bytes]] = []
    now = time.monotonic()
    for item in picked:
        if not isinstance(item, dict):
            raise SiteError("挑好的公告应当是对象列表")
        aid = str(item.get("announcement_id") or "").strip()
        hit = _ANN_CACHE.get(aid)
        if not hit or now - hit[1] > _ANN_CACHE_TTL:
            raise SiteError(f"公告 {aid or '(空)'} 不在这次的候选列表里（或已超过 30 分钟），请重新查一次再挑")
        ann = hit[0]
        role = str(item.get("role") or "") or _default_role(ann.title)
        if role not in IMPORT_ROLES:
            raise SiteError(f"来源类型 {role!r} 不认识，可选：{'、'.join(IMPORT_ROLES)}")
        try:
            entry = _cninfo().download(ann)
        except SourcingError as exc:
            raise SiteError(f"下载「{ann.title}」失败：{exc}") from exc
        path = Path(entry["path"])
        blob = path.read_bytes()
        if len(blob) > MAX_MATERIAL_BYTES:
            raise SiteError(f"「{ann.title}」有 {len(blob) / 1024 / 1024:.0f} MB，超过单份材料上限")
        out.append((path.name, role, ann.published_at, blob))
    return out


def detect_upload(parts: list[tuple[str, str, bytes]]) -> dict:
    """看一眼上传的材料：认出公司/代码/报告期/披露日，并给出合并后的建议值。

    只读不写（不落盘），页面上先把表填好，人再决定要不要导。
    """
    mats = [(fn, payload) for name, fn, payload in parts if fn and name == "material"]
    if not mats:
        raise SiteError("没有可识别的材料：先选 PDF，或者从巨潮挑一份")
    files = []
    for fn, payload in mats:
        if payload[:5] != b"%PDF-":
            files.append({"file_name": fn, "error": "不是 PDF 文件"})
            continue
        try:
            d = report_identity.detect_pdf(payload, fn)
        except Exception as exc:  # noqa: BLE001 - 坏 PDF 不该把整个识别打成 500
            files.append({"file_name": fn, "error": f"读不了这份 PDF：{type(exc).__name__}: {exc}"})
            continue
        d["file_name"] = fn
        files.append(d)

    ok = [f for f in files if not f.get("error")]
    suggestion: dict = {"evidence": {}, "assumed": [], "warnings": []}

    def first(field: str) -> str:
        for f in ok:
            if f.get(field):
                return str(f[field])
        return ""

    for field in ("company", "stock_code", "period"):
        suggestion[field] = first(field)
    dates = [f.get("publication_date") for f in ok if f.get("publication_date")]
    if dates:
        # 取最早的那个：资料截止日的含义是「判到这天为止」，晚了就会被排除，宁可保守
        suggestion["cutoff_date"] = min(dates)
        if len(set(dates)) > 1:
            suggestion["warnings"].append(
                "材料披露日不止一天，资料截止日按最早的那天填（晚于该日的材料会被排除在判定之外）")
    for f in ok:
        if f.get("stock_code_assumed"):
            suggestion["assumed"].append(f"股票代码是从 {f['file_name']} 首页的 6 位数字推的（页面没写「代码：」标签）")
        if f.get("publication_date_assumed"):
            suggestion["assumed"].append(f"{f['file_name']} 的披露日只写到月，按该月最后一天填的")
        if f.get("period_assumed"):
            suggestion["assumed"].append(f"{f['file_name']} 的报告期是按首页单独的年份行推的（封面是散排文本）")
        if f.get("company_basis", "").startswith("材料正文没有写全称"):
            suggestion["assumed"].append(f"{f['file_name']} 正文没写公司全称，公司名用的是证券简称")
    names = [f.get("company_basis") for f in ok if f.get("company_basis")]
    suggestion["basis"] = names[0] if names else ""
    merged_evidence: dict = {}
    for f in ok:  # 第一份材料认出来的依据打底，后面的补空位
        for k, v in (f.get("evidence") or {}).items():
            merged_evidence.setdefault(k, v)
    suggestion["evidence"] = merged_evidence
    return {"files": files, "suggestion": suggestion}


def import_case(parts: list[tuple[str, str, bytes]], *, case_id: str = "") -> dict:
    """把网页上传的材料落盘成一个可跑的样本，返回样本摘要。

    先全部校验、再落盘：校验不过就不该在磁盘上留下半个样本。
    """
    text = {name: payload.decode("utf-8", "replace") for name, fn, payload in parts if not fn}
    files = [(name, fn, payload) for name, fn, payload in parts if fn]

    company = (text.get("company") or "").strip()
    stock_code = re.sub(r"\D", "", text.get("stock_code") or "")[:8]
    period = (text.get("period") or "").strip()
    raw_cutoff = (text.get("cutoff_date") or "").strip()
    split = (text.get("split_mode") or "auto").strip().lower()
    if split not in SPLIT_MODES:
        raise SiteError(f"拆解方式只认 {'/'.join(SPLIT_MODES)}，收到的是 {split!r}")

    # 从巨潮挑的报告：这里先下回来（只认候选列表里登记过的公告号）
    try:
        picked = json.loads(text.get("picked") or "[]")
    except json.JSONDecodeError as exc:
        raise SiteError("挑好的公告表单数据不是合法 JSON") from exc
    if not isinstance(picked, list):
        raise SiteError("挑好的公告应当是数组")
    downloaded = fetch_picked(picked) if picked else []

    # 草稿：上传的文件优先，否则用粘贴的正文
    draft_bytes, draft_from = b"", ""
    for name, fn, payload in files:
        if name == "draft_file" and payload:
            draft_bytes, draft_from = payload, f"上传的 {_safe_name(fn, default='draft.md')}"
            break
    if not draft_bytes:
        pasted = (text.get("draft_text") or "").strip()
        if pasted:
            draft_bytes, draft_from = pasted.encode("utf-8"), "文本框里粘贴的正文"
    if not draft_bytes:
        raise SiteError("没给草稿：上传一个 .md/.txt，或在文本框里粘贴草稿正文")
    if len(draft_bytes) > MAX_DRAFT_BYTES:
        raise SiteError(f"草稿太大（上限 {MAX_DRAFT_BYTES // 1024 // 1024} MB）")

    # (文件名, 字节, 来源类型, 披露日)——后两项空着表示「要用表单里的 roles/pubdates」
    materials: list[tuple[str, bytes, str, str]] = [
        (fn, payload, "", "") for name, fn, payload in files if name == "material"]
    materials += [(n, b, r, p) for n, r, p, b in downloaded]
    if not materials:
        raise SiteError("没给材料：上传一份 PDF，或者从巨潮挑一份报告")
    total = len(draft_bytes) + sum(len(m[1]) for m in materials)
    if total > MAX_IMPORT_BYTES:
        raise SiteError(f"导入内容共 {total / 1024 / 1024:.0f} MB，超过上限 {MAX_IMPORT_BYTES // 1024 // 1024} MB")

    try:
        roles = json.loads(text.get("roles") or "[]")
        pubdates = json.loads(text.get("pubdates") or "[]")
    except json.JSONDecodeError as exc:
        raise SiteError("材料来源类型/披露日的表单数据不是合法 JSON") from exc
    if not isinstance(roles, list) or not isinstance(pubdates, list):
        raise SiteError("材料来源类型/披露日应当是数组")

    staged: list[tuple[str, str, str, bytes, str]] = []  # (文件名, 来源类型, 披露日, 字节, 披露日依据)
    up_i = 0  # roles/pubdates 是按「上传的那几份」给的，从巨潮挑回来的自带来源类型与日期
    for i, (fn, payload, dl_role, dl_pub) in enumerate(materials):
        label = _safe_name(fn, default=f"material_{i + 1}.pdf")
        if not payload:
            raise SiteError(f"材料「{label}」是空文件")
        if payload[:5] != b"%PDF-":
            raise SiteError(f"材料「{label}」不是 PDF 文件（年报请从巨潮资讯网下载 PDF 原件）")
        if len(payload) > MAX_MATERIAL_BYTES:
            raise SiteError(f"材料「{label}」超过 {MAX_MATERIAL_BYTES // 1024 // 1024} MB")
        if dl_role:
            role, pub_raw = dl_role, dl_pub
            pub_basis = "巨潮公告索引里的公告日期"
        else:
            role = str(roles[up_i] if up_i < len(roles) else "original_annual_report")
            pub_raw = (str(pubdates[up_i]).strip()
                       if up_i < len(pubdates) and str(pubdates[up_i]).strip() else "")
            pub_basis = "表单里填的披露日"
            up_i += 1
        if role not in IMPORT_ROLES:
            raise SiteError(f"材料「{label}」的来源类型 {role!r} 不认识，可选：{'、'.join(IMPORT_ROLES)}")
        staged.append((label, role, pub_raw, payload, pub_basis if pub_raw else ""))

    # 表单没填的字段，按材料本身认一遍（导入接口直接用 curl 打也能跑）。
    # 逐份认：缺披露日的那一份要认自己的，不能拿第一份的结果顶替。
    hints: list[dict] = []
    for name, _role, pub_raw, payload, _basis in staged:
        if pub_raw and company and stock_code and period:
            hints.append({})
            continue
        try:
            hints.append(report_identity.detect_pdf(payload, name))
        except Exception:  # noqa: BLE001 - 认不出来不算错，缺的字段照旧报错
            hints.append({})
    hint: dict = hints[0] if hints else {}
    company = company or str(hint.get("company") or "")
    stock_code = stock_code or str(hint.get("stock_code") or "")
    period = period or str(hint.get("period") or "")

    # 逐份定披露日：表单/公告索引给的优先，其次按材料本身认（文件名日期 > 封面/落款）。
    # 认不出就**留空**——留空会被时间门判成「发布日期缺失、按不可用处理」并排除在判定之外。
    # 拿资料截止日顶替等于替这份材料伪造一个它没有的披露时间，绝不做。
    dated: list[tuple[str, str, str, bytes, str]] = []
    for (name, role, pub_raw, payload, pub_basis), h in zip(staged, hints):
        if pub_raw:
            pub, why = _material_date(pub_raw, name), pub_basis
        else:
            pub = str(h.get("publication_date") or "")
            why = str((h.get("evidence") or {}).get("publication_date") or "")
            if pub:
                pub = _material_date(pub, name)
                if h.get("publication_date_assumed"):
                    why += "（只写到月，按该月最后一天推定）"
        dated.append((name, role, pub, payload,
                      why or "未识别出披露日：这份材料会被排除在判定之外"))
    staged = dated

    if not raw_cutoff:
        # 截止日 = 材料里最早的披露日：晚于该日的材料会被排除，宁可保守
        candidates = [s[2] for s in staged if s[2]] or [str(hint.get("publication_date") or "")]
        raw_cutoff = min([c for c in candidates if c], default="")
    if not raw_cutoff:
        raise SiteError("认不出资料截止日：请在「资料截止日」里填一个日期（YYYY-MM-DD）")
    cutoff = _valid_date(raw_cutoff)

    period = period or f"{cutoff[:4]}A"  # 实在认不出报告期，按截止日那年算（并记进 notes）

    cid = _new_case_id(case_id or f"IMP-{stock_code or 'USER'}-{period}")
    target = IMPORTS_DIR / cid
    target.mkdir(parents=True, exist_ok=True)
    IMPORT_CASES_DIR.mkdir(parents=True, exist_ok=True)

    (target / "draft.md").write_bytes(draft_bytes)
    documents = []
    seen: set[str] = set()
    for i, (name, role, pub, payload, pub_basis) in enumerate(staged):
        if name in seen:  # 同名材料（同一份文件传了两次）换个名，别互相覆盖
            name = f"{Path(name).stem}_{i + 1}{Path(name).suffix}"
        seen.add(name)
        (target / name).write_bytes(payload)
        documents.append({
            "source_id": f"{cid.lower()}_doc{i + 1}",
            "file_name": name,
            "source_type": role,
            "publication_date": pub,
            "publication_date_basis": pub_basis,
            "path": _rel_path(target / name),
        })

    case = {
        "case_id": cid,
        "company": company or f"（未填公司名）{stock_code or '自建样本'}",
        "stock_code": stock_code,
        "period": period,
        "prediction_cutoff_date": cutoff,
        "cutoff_rationale": "使用者在网页端导入自有材料时指定的资料截止日；判定不得引用晚于该日的材料。",
        "draft": _rel_path(target / "draft.md"),
        "split_mode": split,
        "origin": "imported",
        "notes": "网页端导入的自建样本，材料由使用者提供；未附真值文件，页面不显示真值对照。",
        "documents": documents,
    }
    note = (text.get("identity_note") or "").strip()
    if note and "选好材料后" not in note:
        case["identity_note"] = note[:600]
    if hint:  # 留痕：这些字段是程序从材料里认出来的，便于事后核对
        case["detected"] = {
            k: hint.get(k) for k in
            ("company", "company_basis", "stock_code", "stock_code_assumed",
             "period", "publication_date", "publication_date_assumed", "source_type")
        }
        case["detected"]["evidence"] = hint.get("evidence") or {}
    (IMPORT_CASES_DIR / f"{cid}.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "case_id": cid,
        "company": case["company"],
        "stock_code": stock_code,
        "period": period,
        "prediction_cutoff_date": cutoff,
        "draft_from": draft_from,
        "draft_bytes": len(draft_bytes),
        "materials": [{"file_name": n, "source_type": r, "publication_date": p,
                       "publication_date_basis": why, "bytes": len(b)}
                      for n, r, p, b, why in staged],
        "warnings": [f"材料「{n}」没有识别出披露日（{why}），这份材料会被排除在判定之外"
                     for n, r, p, b, why in staged if not p],
    }


# ---------------------------------------------------------------------------
# 跑核查
# ---------------------------------------------------------------------------


def _shot_urls(case_id: str, result: dict) -> dict[str, list[dict]]:
    """每条矛盾的证据 → 截图 URL。截图按需生成，这里只给地址。"""
    shots: dict[str, list[dict]] = {}
    for claim in result.get("claims") or []:
        verdict = claim.get("verdict") or {}
        if verdict.get("label") != "CONTRADICTED":
            continue
        items = []
        for i, ev in enumerate(verdict.get("evidence") or []):
            if ev.get("page") is None or ev.get("value") is None:
                continue
            items.append({
                "url": f"/api/shot/{case_id}/{claim['claim_id']}/{i}.png",
                "url_full": f"/api/shot/{case_id}/{claim['claim_id']}/{i}.png?full=1",
                "file_name": ev.get("file_name"),
                "page": ev.get("page"),
                "value": ev.get("value"),
                "quote": ev.get("quote"),
                "unit": ev.get("unit") or "",
            })
        if items:
            shots[claim["claim_id"]] = items
    return shots


# ---------------------------------------------------------------------------
# 草稿拆解：规则版 / 大模型 / 自动
# ---------------------------------------------------------------------------


SPLIT_MODES = {
    "auto": "自动：先按规则拆，一条都拆不出才走大模型",
    "rules": "规则版：确定性，只认「指标 + 数值 + 单位」句式",
    "llm": "大模型拆解：任意写法都能拆，拆解结果落盘复用（判定仍走规则）",
}
CLAIMS_FILE = "claims.json"


def split_mode_of(case: dict) -> str:
    mode = str(case.get("split_mode") or "rules").strip().lower()
    return mode if mode in SPLIT_MODES else "rules"


def _case_dir(case: dict) -> Path | None:
    """导入样本自己的目录（策展样本没有这层目录，拆解缓存也就不落）。"""
    if case.get("origin") != "imported":
        return None
    rel = case.get("draft") or ""
    if not rel:
        return None
    p = Path(rel)
    d = p.parent if p.is_absolute() else (ROOT / p).parent
    return d if d.exists() else None


def _claims_cache_read(case: dict) -> dict | None:
    d = _case_dir(case)
    if not d:
        return None
    path = d / CLAIMS_FILE
    if not path.exists():
        return None
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return got if isinstance(got, dict) else None


def _claims_cache_write(case: dict, mode: str, claims: list[dict], model: str,
                        draft_sha: str) -> None:
    d = _case_dir(case)
    if not d:
        return
    payload = {
        "case_id": case.get("case_id"),
        "mode": mode,
        "how": "llm",
        "model": model,
        "engine": draft_review.ENGINE_VERSION,
        "prompt": draft_review.PROMPT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "draft_sha256": draft_sha,
        "count": len(claims),
        "note": ("大模型只参与这一次拆解。之后每次运行都复用这份主张，判定照旧走本地规则，"
                 "所以同一份草稿重跑结果逐字相同。换草稿或重新导入会自动重拆。"),
        "claims": claims,
    }
    (d / CLAIMS_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _llm_model() -> str:
    try:
        return str((draft_review.llm.load_config() or {}).get("model") or "")
    except Exception:  # noqa: BLE001 - 只是展示用，模型名取不到不该让核查失败
        return ""


def split_draft(case: dict, *, resplit: bool = False) -> tuple[list[dict], dict]:
    """把草稿拆成主张。返回 (主张, 拆解信息)；大模型最多真跑一次，结果落盘复用。"""
    mode = split_mode_of(case)
    info: dict = {"mode": mode, "how": "", "model": "", "from_": "", "degrade": "", "upgrade": ""}
    rel = case.get("draft") or ""
    path = Path(rel) if Path(rel).is_absolute() else ROOT / rel
    if not path.exists():
        raise SiteError(f"草稿文件不在：{rel}")
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if not resplit:
        cached = _claims_cache_read(case)
        # 拆解结果只有「草稿正文 + 拆解模式 + 引擎/提示词版本」全一致才复用：
        # 换了提示词或引擎版本，同一份草稿拆出来的主张会变，旧的不能当新的用。
        if (cached and cached.get("draft_sha256") == sha and cached.get("mode") == mode
                and cached.get("engine") == draft_review.ENGINE_VERSION
                and cached.get("prompt") == draft_review.PROMPT_VERSION):
            info.update(how=cached.get("how") or "llm", model=cached.get("model") or "",
                        from_="cache")
            return [dict(c) for c in (cached.get("claims") or [])], info

    if mode != "llm":
        got = draft_review.extract_claims_rules(text)
        if got or mode == "rules":
            info.update(how="rules", from_="fresh")
            return got, info
        info["upgrade"] = "规则版一条主张都没拆出来，这份草稿改用大模型拆解"
    try:
        claims, how, degrade = draft_review.split_claims(
            text, use_llm=True, source=Path(rel).name)
    except Exception as exc:  # noqa: BLE001 - split_claims 自己会降级，这里是最后兜底
        return [], {**info, "how": "rules", "from_": "fresh", "degrade": f"拆解异常：{exc}"}
    info.update(how=how, from_="fresh", degrade=degrade or "",
                model=_llm_model() if how == "llm" else "")
    if how == "llm":
        try:
            _claims_cache_write(case, mode, claims, info["model"], sha)
        except OSError:
            tail = "拆解结果没能落盘：下次运行会重新调用模型"
            info["degrade"] = f"{info['degrade']}；{tail}" if info["degrade"] else tail
    return claims, info


def run_case(case_id: str, *, refresh: bool = False, resplit: bool = False,
             on_event=None) -> dict:
    """跑核查并组装页面要的全部信息。

    on_event：可选回调，透传过程中的真实进度（材料登记/建索引/逐条判定）给界面。
    给了它就不用运行缓存，保证事件每次都真的走一遍。

    结果缓存按「产物指纹」命中：原始输入或**本次实际采用的主张集**变了就重算
    （重新拆解拆出不同主张时，原始输入一个字节都没变 —— 只比输入的话，结果、截图、
    报告会一起停在上一轮）。resplit 是显式要求重拆草稿，也绕过结果缓存。
    """
    case = load_case(case_id)
    missing = [str(p) for _, _, p in material_paths(case) if not p.exists()]
    if missing:
        raise SiteError(
            "材料未就位，无法核查：" + "、".join(Path(m).name for m in missing)
            + "（本地年报 PDF 不入库，需先跑 cli fetch 或从 data/sourced 复制）"
        )
    claims, split_info = split_draft(case, resplit=resplit)
    input_fp = case_input_fingerprint(case_id, case)
    artifact_fp = case_artifact_fingerprint(case_id, case, claims, input_fp=input_fp)
    if on_event is None and not refresh and not resplit:
        hit = _RUN_CACHE.get(case_id)
        if hit and hit[0] == artifact_fp:
            return hit[1]
    result = draft_review.run_draft_case(
        case, use_llm=False, claims_override=claims, claims_how=split_info["how"] or "rules",
        on_event=on_event)

    data = result.to_dict()
    for note in (split_info.get("upgrade"), split_info.get("degrade")):
        if note:
            data.setdefault("warnings", []).insert(0, note)
    data["split"] = {
        "mode": split_info["mode"],
        "mode_label": SPLIT_MODES.get(split_info["mode"], split_info["mode"]),
        "how": split_info["how"],
        "model": split_info["model"],
        "from": split_info["from_"],
        "llm_used": split_info["how"] == "llm",
        "note": ("本次拆解经过大模型（模型只跑这一次，主张已落盘复用）；判定仍走本地规则，"
                 "不经过大模型。" if split_info["how"] == "llm" else "拆解与判定都在本地完成。"),
    }
    # 显式给出每份材料的绝对路径：build_pdf 会优先用它，避免同名文件解析到别家
    data["documents"] = [
        {"file_name": name, "source_type": st, "path": str(path)}
        for name, st, path in material_paths(case)
    ]
    data["shots"] = _shot_urls(case_id, data)
    data["report_url"] = f"/api/report/{case_id}.pdf"
    data["gold"] = _gold_summary(case)
    data["rules_only"] = split_info["how"] != "llm"
    # 截图缓存与报告 PDF 都按这一份产物指纹校验，三类产物不会互相打架
    data["input_fingerprint"] = input_fp
    data["artifact_fingerprint"] = artifact_fp
    _RUN_CACHE[case_id] = (artifact_fp, data)
    return data


# ---------------------------------------------------------------------------
# 截图 / PDF
# ---------------------------------------------------------------------------


SHOT_PREVIEW_WIDTH = 760


def shot_png(case_id: str, claim_id: str, index: int, *, full: bool = False) -> bytes:
    data = run_case(case_id)                       # 命中缓存则很快，同时给出本轮产物指纹
    # 键里带产物指纹（含本次采用的主张集）：重新拆解后条号可能还是 DRAFT-C001…，
    # 只有指纹能说明这张图属于哪一轮，否则会把上一轮的红框当这一轮的返回
    key = (case_id, str(data.get("artifact_fingerprint") or ""), claim_id, index, bool(full))
    if key in _SHOT_CACHE:
        return _SHOT_CACHE[key]
    claim = next((c for c in data["claims"] if c["claim_id"] == claim_id), None)
    if claim is None:
        raise SiteError(f"样本里没有 {claim_id}")
    evidence = (claim.get("verdict") or {}).get("evidence") or []
    if not 0 <= index < len(evidence):
        raise SiteError(f"{claim_id} 没有第 {index} 条证据")
    ev = evidence[index]
    case = load_case(case_id)
    docs = {name: p for name, _st, p in material_paths(case)}
    path = docs.get(str(ev.get("file_name"))) or (_material_dir(case) / str(ev.get("file_name")))
    if not path.exists():
        raise SiteError(f"材料不在本地：{path.name}")
    png = pdf_report.render_shot(path, ev.get("page"), ev.get("value"),
                                 max_width=None if full else SHOT_PREVIEW_WIDTH)
    if png is None:
        raise SiteError(f"{claim_id} 的数值在原文里定位不到，无法截屏")
    while len(_SHOT_CACHE) >= SHOT_CACHE_MAX:
        _SHOT_CACHE.pop(next(iter(_SHOT_CACHE)))  # dict 保序：丢最早那张
    _SHOT_CACHE[key] = png
    return png


#: 报告 PDF / 截图的输入指纹写在这个同名前缀的小文件里。
#: 只判断「PDF 在不在」是不够的：重跑、重新拆解、换了草稿或材料之后，下载到的仍是旧报告。
_FP_SUFFIX = ".fingerprint"


def case_input_fingerprint(case_id: str, case: dict | None = None) -> str:
    """这份样本「原始输入」的指纹：案件字段 + 草稿正文 + 材料原件 + 引擎/提示词版本。

    它是产物指纹（`case_artifact_fingerprint`）的前半段；三类产物实际用产物指纹判断
    能不能复用 —— 那一份还包含本次**实际采用的主张集**。

    覆盖范围取「整个案例 JSON 的规范化哈希」，不做逐字段白名单 —— 白名单漏掉一个字段
    （比如以后新增的判定参数）就会重新长出同一类缺陷，这里宁可多算一次。草稿按**正文内容**
    哈希（不是路径），材料按**原件内容哈希**算（见 draft_review._file_fingerprint：
    只靠大小/时间戳在 Windows 上认不出同 tick 内的改写），所以改内容、换文件、
    重新导出同尺寸文件都能认出来。
    """
    case = case if case is not None else load_case(case_id)
    h = hashlib.sha256()
    h.update(json.dumps(case, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    h.update(f"|{draft_review.ENGINE_VERSION}|{draft_review.PROMPT_VERSION}"
             f"|{split_mode_of(case)}".encode("utf-8"))
    draft = str(case.get("draft") or "")
    dpath = Path(draft) if Path(draft).is_absolute() else ROOT / draft
    try:
        h.update(hashlib.sha256(dpath.read_bytes()).hexdigest().encode("utf-8"))
    except OSError:
        h.update(b"draft-missing")
    for name, _st, path in material_paths(case):
        h.update(f"{name}|{path}".encode("utf-8"))
        h.update(draft_review._file_fingerprint(path).encode("utf-8"))
    return h.hexdigest()[:16]


def _digest(value: object) -> str:
    """任意结构的规范化摘要（键排序），用来把「本次实际采用的主张集」纳入产物指纹。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]


def case_artifact_fingerprint(case_id: str, case: dict | None = None,
                              claims: list[dict] | None = None, *,
                              input_fp: str | None = None) -> str:
    """产物指纹 = 原始输入指纹 + **本次实际采用的主张集**摘要。

    只覆盖原始输入是不够的：同一份草稿重新拆解（resplit，或换了模型/提示词）可能拆出不同
    条数、不同内容的主张，而原始输入一个字节都没变 —— 这时结果、截图、报告 PDF 必须跟着变，
    否则红框与逐条核查指向的是上一轮的主张（实测：拆解结果 14 条变 13 条，指纹不变、
    报告不重建，下载到的还是旧 PDF）。
    claims 不给就按「当前会采用的主张」取：走落盘缓存的文件，不重新调模型。
    """
    case = case if case is not None else load_case(case_id)
    if claims is None:
        claims, _info = split_draft(case)
    fp = input_fp or case_input_fingerprint(case_id, case)
    return f"{fp}-{_digest(claims)}"


def report_pdf(case_id: str, *, rebuild: bool = False) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id)
    out = CACHE_DIR / f"{safe}.pdf"
    fp_file = CACHE_DIR / f"{safe}{_FP_SUFFIX}"
    # 先取本轮的产物指纹（含本次实际采用的主张集）：命中结果缓存时几乎免费，同时保证
    # 「重新拆解改了主张 → 报告重建」。案例或材料有问题就照常在这里报错，不静默。
    data = run_case(case_id)
    want = str(data.get("artifact_fingerprint") or "")
    if out.exists() and not rebuild and want:
        try:
            cached = fp_file.read_text(encoding="utf-8").strip()
        except OSError:
            cached = ""
        if cached == want:
            return out
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pdf_report.build_pdf(data, out, raw_root=RAW_DIR)
    if want:
        fp_file.write_text(want, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "fintrace-site/1.0"

    # -- 工具 --
    # 手机走隧道访问时，未压缩的 67 KB 静态资源 + 每次 no-store 重下就是主要延迟来源，
    # 所以：文本一律按需 gzip，静态资源带 ETag（重访只回 304 或直接用缓存）。
    COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg")

    def _send(self, code: int, body: bytes, ctype: str, *,
              cache: str = "no-store", etag: str | None = None) -> None:
        headers: list[tuple[str, str]] = [("Content-Type", ctype)]
        if etag:
            headers.append(("ETag", etag))
        headers.append(("Cache-Control", cache))
        if (len(body) > 512
                and any(ctype.startswith(t) for t in self.COMPRESSIBLE)
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()):
            body = gzip.compress(body, 6)
            headers.append(("Content-Encoding", "gzip"))
            headers.append(("Vary", "Accept-Encoding"))
        self.send_response(code)
        for key, val in headers:
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_modified(self, etag: str) -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self, length: int) -> bytes:
        body = self.rfile.read(length) if length > 0 else b""
        self._body_consumed = getattr(self, "_body_consumed", 0) + len(body)
        return body

    def _drain_body(self, *, cap: int = MAX_IMPORT_BYTES + 1024 * 1024) -> None:
        """回错误前把没读完的请求体读掉。

        不读会怎样：客户端还在上传时就收到响应、连接被关，客户端报的是
        「连接被中止」（Windows 10053），而不是我们写清楚的那句错误。

        cap 取「我们自己前端可能发上来的最大体积」（导入材料那道 200 MB 的口子），
        比它小的都读干净再回错；否则超限那条错误照样会变成客户端侧的连接中断。
        """
        try:
            left = int(self.headers.get("Content-Length") or 0) - getattr(self, "_body_consumed", 0)
        except ValueError:
            return
        if 0 < left <= cap:
            try:
                self.rfile.read(left)
                # 这里必须是「读改写」：超限那条路上 _read_body 一次都没被调用过，
                # 属性还不存在，直接 += 会抛 AttributeError，错误就发不出去了。
                self._body_consumed = getattr(self, "_body_consumed", 0) + left
            except OSError:
                pass

    def _err(self, code: int, msg: str) -> None:
        if self.command == "POST":
            self._drain_body()
        self._json({"error": msg}, code)

    def _static(self, rel: str) -> None:
        rel = unquote(rel).lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._err(403, "路径越界")
            return
        if not target.is_file() and not target.suffix:
            # 干净的 URL：/app → web/app.html（不暴露 .html 后缀）
            alt = target.with_suffix(".html")
            if alt.is_file():
                target = alt
        if not target.is_file():
            self._err(404, f"没有这个文件：{rel}")
            return
        stat = target.stat()
        etag = f'W/"{int(stat.st_mtime)}-{stat.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self._not_modified(etag)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        rel_posix = target.relative_to(WEB_DIR.resolve()).as_posix()
        # 资源文件允许短缓存 + 后台校验（改完文件一分钟后自动生效，不用清缓存）；
        # 页面本身每次校验（ETag 命中就 304，很便宜）。
        cache = ("public, max-age=60, stale-while-revalidate=600"
                 if rel_posix.startswith("assets/") else "no-cache")
        self._send(200, target.read_bytes(), ctype, cache=cache, etag=etag)

    # -- 路由 --
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        url = urlparse(self.path)
        path = url.path
        try:
            if path.startswith("/api/"):
                self._api(path, parse_qs(url.query))
            else:
                self._static(path)
        except SiteError as exc:
            self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - 演示服务，别把栈打给前端
            self._err(500, f"内部错误：{type(exc).__name__}: {exc}")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # /api/import：导入自有材料（multipart 上传）
            if url.path == "/api/import":
                if length > MAX_IMPORT_BYTES + 1024 * 1024:
                    raise SiteError(f"上传内容超过上限 {MAX_IMPORT_BYTES // 1024 // 1024} MB")
                parts = parse_multipart(self.headers.get("Content-Type") or "", self._read_body(length))
                info = import_case(parts)
                _RUN_CACHE.clear()  # 样本表变了，缓存不留旧的
                forget_case(info["case_id"])
                sample = next((s for s in list_samples() if s["case_id"] == info["case_id"]), None)
                self._json({"ok": True, "imported": info, "sample": sample})
                return
            if url.path == "/api/detect":
                if length > MAX_IMPORT_BYTES + 1024 * 1024:
                    raise SiteError(f"上传内容超过上限 {MAX_IMPORT_BYTES // 1024 // 1024} MB")
                parts = parse_multipart(self.headers.get("Content-Type") or "", self._read_body(length))
                self._json(detect_upload(parts))
                return
            if url.path == "/api/chat":
                # 单输入框对话：请求体是 JSON，响应是 NDJSON 事件流（一行一个事件）
                if length > CHAT_MAX_BYTES:
                    raise SiteError(f"消息太长（上限 {CHAT_MAX_BYTES // 1024 // 1024} MB）")
                self._chat(json.loads(self._read_body(length) or b"{}"))
                return
            if url.path != "/api/run":
                self._err(404, f"没有这个接口：{url.path}")
                return
            body = json.loads(self._read_body(length) or b"{}")
            case_id = str(body.get("case_id") or "")
            if not case_id:
                raise SiteError("缺 case_id")
            self._json(run_case(case_id, refresh=bool(body.get("refresh"))))
        except SiteError as exc:
            self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"内部错误：{type(exc).__name__}: {exc}")

    def _chat(self, body: dict) -> None:
        """单输入框对话：把智能体外壳的事件流按 NDJSON 逐条推给前端。

        用 HTTP/1.0 且不带 Content-Length 的流式写法：事件发完连接关闭，前端读到 EOF
        就算这一轮结束（不用 EventSource，它断线会自动重连，反而添乱）。
        """
        from . import agent_shell

        sess = agent_shell.get_session(str(body.get("session") or ""))
        message = str(body.get("message") or "")
        draft_text = str(body.get("draft_text") or "")
        draft_name = str(body.get("draft_name") or "")
        b64 = str(body.get("draft_b64") or "")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def push(ev: dict) -> None:
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        if b64:  # 上传的草稿文件：服务端解出正文（.pdf 走 pymupdf，md/txt 直接解码）
            import base64
            try:
                draft_text = agent_shell.draft_text_from_bytes(
                    base64.b64decode(b64), draft_name)
                draft_name = draft_name or "上传的草稿.pdf"
            except Exception as exc:  # noqa: BLE001 - 解不开就如实说，别装作读到了
                try:
                    push({"kind": "error", "text": f"草稿文件读不开：{exc}"})
                except OSError:
                    pass
                return
        try:
            for ev in agent_shell.respond(sess, message, draft_text=draft_text,
                                          draft_name=draft_name):
                push(ev)
        except (BrokenPipeError, ConnectionResetError):
            return  # 前端提前关了页面，正常
        except Exception as exc:  # noqa: BLE001 - 流已开头，错只能当事件发出去
            try:
                push({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            except OSError:
                pass

    def _api(self, path: str, query: dict) -> None:
        parts = [p for p in path.split("/") if p]
        # /api/samples
        if parts == ["api", "samples"]:
            self._json({"samples": list_samples()})
            return
        # /api/run?case_id=...
        if parts == ["api", "run"]:
            case_id = (query.get("case_id") or [""])[0]
            if not case_id:
                raise SiteError("缺 case_id")
            on = lambda k: (query.get(k) or [""])[0] in ("1", "true", "yes")  # noqa: E731
            self._json(run_case(case_id, refresh=on("refresh"), resplit=on("resplit")))
            return
        # /api/lookup?q=天下秀
        if parts == ["api", "lookup"]:
            q = (query.get("q") or [""])[0]
            self._json({"query": q, "companies": lookup_companies(q)})
            return
        # /api/reports?code=600556&kind=annual
        if parts == ["api", "reports"]:
            code = (query.get("code") or [""])[0]
            kind = (query.get("kind") or ["annual"])[0] or "annual"
            self._json({"code": code, "kind": kind, "kind_label": REPORT_KINDS.get(kind, kind),
                        "reports": list_reports(code, kind)})
            return
        # /api/shot/<case>/<claim>/<i>.png
        if len(parts) == 5 and parts[:2] == ["api", "shot"]:
            claim_id, tail = parts[3], parts[4]
            if not tail.endswith(".png") or not tail[:-4].isdigit():
                raise SiteError("截图地址形如 /api/shot/<样本>/<主张>/<序号>.png")
            full = (query.get("full") or [""])[0] in ("1", "true", "yes")
            self._send(200, shot_png(parts[2], claim_id, int(tail[:-4]), full=full),
                       "image/png", cache="no-store")
            return
        # /api/report/<case>.pdf
        if len(parts) == 3 and parts[:2] == ["api", "report"]:
            if not parts[2].endswith(".pdf"):
                raise SiteError("报告地址形如 /api/report/<样本>.pdf")
            # PDF 按样本号缓存在 out/_site/ 下：改了判定代码之后不重出就会端出上一份
            # （修复前生成的报告会一直挂在同一个地址上）。允许显式要求重出。
            rebuild = (query.get("rebuild") or [""])[0] in ("1", "true", "yes")
            out = report_pdf(parts[2][:-4], rebuild=rebuild)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(out.stat().st_size))
            self.send_header("Content-Disposition", f'inline; filename="{out.name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(out.read_bytes())
            return
        self._err(404, f"没有这个接口：{path}")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 演示服务：默认静默，需要时设 FINTRACE_SITE_LOG=1
        import os
        if os.environ.get("FINTRACE_SITE_LOG"):
            super().log_message(fmt, *args)


def _port_busy(host: str, port: int) -> bool:
    """端口上已经有东西在应答。

    Windows 上 SO_REUSEADDR 会让第二次 bind 也成功，于是两个服务抢同一个端口、
    请求被老进程接走——表现为「改了代码没生效」。宁可启动即失败，也不静默分裂。
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.8)
        try:
            probe.connect((host, port))
        except OSError:
            return False
    return True


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """建好一个已绑定、但还没开始服务的实例（测试也用它，传 port=0 取空闲端口）。

    Windows 上 SO_REUSEADDR 会让第二个进程也 bind 成功，于是两个服务抢一个端口、
    请求被老进程接走——表现为「改了代码没生效」，所以默认关掉。
    代价是刚停掉服务时旧连接处于 TIME_WAIT，重启会 bind 失败；这时先探一下：
    没人应答说明只是 TIME_WAIT，才允许放开 SO_REUSEADDR 重试一次。
    """
    if not WEB_DIR.is_dir():
        raise SystemExit(f"找不到前端目录：{WEB_DIR}")
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError:
        if port == 0 or _port_busy(host, port):
            raise
        ThreadingHTTPServer.allow_reuse_address = True
        try:
            return ThreadingHTTPServer((host, port), Handler)
        finally:
            ThreadingHTTPServer.allow_reuse_address = False


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
          open_browser: bool = True) -> None:
    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("只允许监听 127.0.0.1（项目红线：不对外网开放）")
    if _port_busy(host, port):
        raise SystemExit(
            f"端口 {port} 已被占用（该端口上已有服务在应答）。\n"
            f"  先关掉它：netstat -ano | findstr :{port} 找到 PID，再 taskkill /F /PID <PID>\n"
            f"  或换一个端口：--port {port + 1}"
        )
    httpd = make_server(host, port)
    url = f"http://{host}:{port}/"
    print(f"演示站点已启动：{url}")
    print("  页面上的结论由本地引擎现算（规则版，不经过大模型）；Ctrl+C 退出。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
