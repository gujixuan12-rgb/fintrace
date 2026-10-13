"""步骤 ⑨：可交付 PDF 报告 —— 判定结论 + 原文高亮截屏 + 内链跳转。

输入就是 `draft_review.DraftReviewResult.to_dict()`（CLI 落盘的 `out/<run>/payload.json`）。
输出的 PDF 分四段：

    封面与边界声明 → 目录（可点击跳转）→ 逐条核查（逻辑链 + 原文红框截屏）→ 材料清单

几条踩过的工程约束，改这个文件时别踩回去：

* 分页必须用 `page-break-before: always`；`break-before: page` 在 Story 里不生效。
* 图片进 Story 要把文件放进 `pymupdf.Archive` 指向的目录，再用相对 `src` 引用。
* 内链靠 `Story.write_with_links()`：正文里 `<a href="#名字">` 配 `id="名字"`，
  它会把这些锚点写成 PDF 的 GOTO 链接（`add_pdf_links` 是同一机制的低层入口）。
* 中文字体走 CSS `@font-face`（把 msyh.ttc 放进档案目录）。写盘后要调
  `Document.subset_fonts()` 再去保存，否则一份报告会膨胀到十几 MB。
* 判断截图有没有落页，用 `page.get_image_info()`；`page.get_images()` 看不到
  Story 放进去的图。

pymupdf 是可选依赖（`pip install -e ".[pdf]"`），只在真正生成报告时才导入。
"""

from __future__ import annotations

import html as html_mod
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

def _project_root() -> Path:
    """项目根目录。本模块在 reporting/ 子包下，层级比 src/fintrace/*.py 深一层，
    所以不写死 parents[N]，而是往上找到带 pyproject.toml 的那一级。"""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists():
            return p
    return here.parents[3]


ROOT = _project_root()

#: 判定标签 → 中文与配色类名（与 draft_review 的枚举一一对应）。
#: PREDICTION 是第四类「预测/观点（非披露事实）」：单列一行、不进核查分母。
LABEL_CN = {"SUPPORTED": "支持", "CONTRADICTED": "矛盾", "INSUFFICIENT": "证据不足",
            "PREDICTION": "预测/观点"}
LABEL_CLASS = {"SUPPORTED": "ok", "CONTRADICTED": "bad", "INSUFFICIENT": "unk",
               "PREDICTION": "pred"}

#: 六大错误类型的中文名。报告里两种写法都给，便于人工核对。
ERROR_TYPE_CN = {
    "numeric_scale": "数量级错误",
    "numeric_mismatch": "数值不符",
    "unit": "单位错误",
    "citation_page": "引用页码错误",
    "caliber": "口径不符",
    "cross_reference": "跨页勾稽不一致",
}

CJK_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)

CSS = """
@font-face {font-family: 'CJK'; src: url(cjk.ttc);}
body {font-family: 'CJK'; font-size: 10pt; line-height: 1.62; color: #1b1b1f;}
h1 {font-size: 21pt; margin: 0 0 6pt 0; color: #101014;}
h2 {font-size: 14pt; margin: 16pt 0 6pt 0; color: #7a1020;
    border-bottom: 1pt solid #d9d9de; padding-bottom: 3pt;}
h3 {font-size: 12pt; margin: 14pt 0 4pt 0;}
.cover {margin-top: 90pt;}
.cover .sub {font-size: 11pt; color: #5a5a63; margin-top: 4pt;}
.meta {margin-top: 14pt; padding: 9pt 11pt; background: #f5f5f7; border-radius: 4pt;}
.meta div {margin: 2pt 0;}
.callout {margin-top: 12pt; padding: 9pt 11pt; border-left: 3pt solid #7a1020;
          background: #fdf3f4; font-size: 9pt; color: #4a3a3d;}
/* 材料窗口覆盖不到草稿期间时的顶部告警：这一条必须比正文更早被看见 */
.coverage-alert {margin-top: 12pt; padding: 9pt 11pt; border: 1.2pt solid #b3261e;
                 background: #fdecea; font-size: 9.5pt; color: #7a1c15; line-height: 1.55;}
ol.toc {margin: 6pt 0 0 0; padding-left: 16pt;}
ol.toc li {margin: 2.5pt 0; font-size: 9.5pt;}
a {color: #17458f; text-decoration: none;}
.tag {font-size: 9pt; padding: 1pt 5pt; border-radius: 3pt; color: #fff;}
.tag.ok {background: #1d7a44;}
.tag.bad {background: #b32a26;}
.tag.unk {background: #6b6b73;}
.tag.pred {background: #5b5b8a;}
.claim {margin: 4pt 0; padding: 6pt 9pt; background: #f2f5fb; border-radius: 3pt;}
.chain {margin: 6pt 0; padding: 7pt 10pt; border: 0.7pt solid #d5d5db;
        background: #fbfbfc; border-radius: 3pt; font-size: 9.5pt;}
.chain .k {color: #7a1020; font-weight: bold;}
.fix {margin: 5pt 0; padding: 6pt 9pt; background: #f0f8f2; border-radius: 3pt; font-size: 9.5pt;}
figure {margin: 7pt 0 10pt 0;}
figure img {width: 100%; border: 0.6pt solid #cccccc;}
figcaption {font-size: 8.5pt; color: #5a5a63; margin-top: 3pt;}
.back {font-size: 8.5pt; color: #5a5a63;}
.muted {color: #5a5a63; font-size: 9pt;}
table {width: 100%; border-collapse: collapse; font-size: 9pt;}
th, td {border: 0.6pt solid #d5d5db; padding: 4pt 6pt; text-align: left;}
th {background: #f0f0f3;}
"""


def _pymupdf():
    """延迟导入 pymupdf，缺依赖时给出可执行的安装提示。"""
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError('生成 PDF 报告需要 pymupdf，请先安装：pip install -e ".[pdf]"') from exc
    return pymupdf


# ---------------------------------------------------------------------------
# 在原文里定位「出错的那串数字」
# ---------------------------------------------------------------------------


def _digits(s: str) -> str:
    """只留数字和小数点。"""
    return re.sub(r"[^\d.]", "", s or "")


def _digits_signed(s: str) -> str:
    """只留数字、小数点与正负号。

    负号不能丢：-48.43 若被拍成 48.43，就会先命中页面上那个**同值的正数**，
    红框落在错的地方（比不画框更糟）。统一把各种减号写法归一成 '-'。
    """
    out: list[str] = []
    for ch in s or "":
        if ch in "-−－":
            out.append("-")
        elif ch.isdigit() or ch == ".":
            out.append(ch)
    return "".join(out)


def _value_text(value: Any) -> str:
    """把数值写成纯数字串（1,243,188,170.21 → '1243188170.21'）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _fmt_num(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{int(v):,}" if v == int(v) and abs(v) < 1e15 else f"{v:,.4f}".rstrip("0").rstrip(".")


def find_value_rects(page, value: Any, *, limit: int = 3) -> list:
    """在页面上找出与该数值等值的文本位置。

    按「词流里的数字窗口」匹配，而不是拿 quote 直接搜：年报里一个数字常被排成
    两行（`1,243,188,17` / `0.21`），直接搜整串会落空。先按含小数点的形式匹配，
    再退回纯数字匹配。

    **连正负号一起比**：目标值是负数时只认带负号的写法，不再退到不带号的匹配 ——
    否则会命中间一页上同值的正数，画出误导性的红框。找不到就返回空（宁可不画）。
    """
    target = _digits_signed(_value_text(value))
    if len(target.lstrip("-")) < 2:
        return []
    pymupdf = _pymupdf()
    words = [w for w in page.get_text("words") if _digits(w[4])]
    words.sort(key=lambda w: (w[5], w[6], w[7]))
    frags = [(w[4], _digits_signed(w[4]), pymupdf.Rect(w[0], w[1], w[2], w[3]))
             for w in words]

    targets = [target]
    flat = target.replace(".", "")
    if flat != target and len(flat.lstrip("-")) >= 4:
        targets.append(flat)

    found: list = []
    for t in targets:
        for size in range(1, min(8, len(frags)) + 1):
            for i in range(len(frags) - size + 1):
                if "".join(f[1] for f in frags[i:i + size]) != t:
                    continue
                rect = frags[i][2]
                for f in frags[i + 1:i + size]:
                    rect = rect | f[2]
                if any(rect.intersects(x) for x in found):
                    continue
                found.append(rect)
                if len(found) >= limit:
                    return found
        if found:
            break
    return found


def render_shot(pdf_path: str | Path, page_no: Any, value: Any, *,
                dpi: int = 190, pad_x: float = 24.0, pad_y: float = 44.0,
                max_width: int | None = None) -> bytes | None:
    """截出「出错处」所在的原文区域（红框标出目标数值）。定位不到就返回 None。

    max_width：按裁切宽度反算 dpi，给网页内嵌缩略图用。190dpi 的全图约 54 KB/张，
    手机走隧道一次要下 8 张，缩到 ~760px 宽约 1/3 大小；点开看原尺寸时再要全图。
    """
    pymupdf = _pymupdf()
    path = Path(pdf_path)
    if not path.exists():
        return None
    try:
        page_no = int(page_no)
    except (TypeError, ValueError):
        return None
    with pymupdf.open(str(path)) as doc:
        if not 1 <= page_no <= doc.page_count:
            return None
        page = doc[page_no - 1]
        rects = find_value_rects(page, value)
        if not rects:
            return None
        core = rects[0]
        # 裁切到整栏宽度，上下各留一段，让人看清这是哪个表格的哪一行
        clip = pymupdf.Rect(page.rect.x0 + pad_x, core.y0 - pad_y,
                            page.rect.x1 - pad_x, core.y1 + pad_y)
        clip = clip & page.rect
        if clip.height < 70:
            clip.y0 = max(page.rect.y0, core.y1 - 70)
        for r in rects[:3]:
            page.draw_rect(r, color=(0.85, 0.10, 0.10), width=1.4)
        if max_width:
            # 72dpi 时 1pt = 1px，据此把 dpi 压到目标宽度
            dpi = max(72, min(dpi, int(max_width * 72.0 / max(clip.width, 1.0))))
        return page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")


# ---------------------------------------------------------------------------
# HTML 组装
# ---------------------------------------------------------------------------


def _esc(s: Any) -> str:
    return html_mod.escape(str(s if s is not None else ""))


def _error_type_cn(et: Any) -> str:
    if not et:
        return ""
    return ERROR_TYPE_CN.get(et, str(et))


def _doc_index(result: dict, raw_root: Path) -> dict[str, Path]:
    """文件名 → 本地路径。优先用 payload 里的 documents，否则在 data/raw 下按名找。"""
    index: dict[str, Path] = {}
    for d in result.get("documents") or []:
        name, p = d.get("file_name"), d.get("path")
        if name and p:
            cand = Path(p)
            index[name] = cand if cand.is_absolute() else ROOT / p
    if raw_root.exists():
        for p in raw_root.rglob("*.pdf"):
            index.setdefault(p.name, p)
    return index


def _clip(s: str, n: int = 160) -> str:
    """展示用截断：表格类原句可能有几千字，报告里只留开头。"""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def _claim_head(claim: dict) -> str:
    """一条主张的抬头：先给断言（指标 值 单位），原句不同再附原文。

    规则拆出来的主张原文常是整句话，同一句话里的两个指标会显得像重复主张；
    抬头给断言、并把「同句共 N 条」说清楚，就不会被误读成重复。
    """
    asrt = str(claim.get("assertion") or claim.get("claim_text") or "")
    text = str(claim.get("claim_text") or "")
    head = _esc(asrt)
    if asrt and text and asrt != text:
        head += f'　<span class="muted">（原句：{_esc(_clip(text))}）</span>'
    n = claim.get("group_size") or 1
    if n > 1:
        head += (f'　<span class="muted">（同句第 {claim.get("claim_group_id") or "?"} 组，'
                 f'共 {n} 条断言）</span>')
    return head


def _chain_html(claim: dict) -> str:
    """逻辑链：主张 → 材料披露 → 对照依据 → 结论。对照依据用系统自己的判定文本。"""
    v = claim["verdict"]
    ev = v.get("evidence") or []
    rows = [f'<div><span class="k">① 主张</span>：{_claim_head(claim)}</div>']
    if ev and ev[0].get("value") is not None:
        e = ev[0]
        page = f'第 {e["page"]} 页' if e.get("page") else "页码未取到"
        rows.append(
            f'<div><span class="k">② 材料披露</span>：{_fmt_num(e["value"])}'
            f'{_esc(e.get("unit") or "")}（{_esc(e.get("file_name"))}　{page}）</div>')
    elif ev:
        e = ev[0]
        page = f'第 {e["page"]} 页' if e.get("page") else "页码未取到"
        rows.append(f'<div><span class="k">② 材料位置</span>：{_esc(e.get("file_name"))}　{page}</div>')
    rows.append(f'<div><span class="k">③ 对照</span>：{_esc(v.get("basis"))}</div>')
    conclusion = f'判定：{LABEL_CN.get(v["label"], v["label"])}'
    if v.get("error_type"):
        conclusion += f'　错误类型：{_error_type_cn(v["error_type"])}（{_esc(v["error_type"])}）'
    if v.get("confidence") is not None:
        conclusion += f'　置信度：{v["confidence"]}'
    rows.append(f'<div><span class="k">④ 结论</span>：{conclusion}</div>')
    return '<div class="chain">' + "".join(rows) + "</div>"


def _claim_html(claim: dict, shots: dict[str, list[tuple[str, str]]]) -> str:
    v = claim["verdict"]
    label = v["label"]
    cid = _esc(claim["claim_id"])
    out = [f'<h3 id="{cid}">{cid}　'
           f'<span class="tag {LABEL_CLASS.get(label, "unk")}">{LABEL_CN.get(label, label)}</span>'
           + (f'　<span class="muted">错误类型：{_error_type_cn(v.get("error_type"))}</span>'
              if v.get("error_type") else "")
           + "</h3>"]
    out.append(_chain_html(claim))
    if v.get("suggested_fix"):
        out.append(f'<div class="fix"><b>修改建议</b>：{_esc(v["suggested_fix"])}</div>')
    if v.get("unable_reason") and v["unable_reason"] != v.get("basis"):
        out.append(f'<div class="muted">无法判断原因：{_esc(v["unable_reason"])}</div>')
    if v.get("missing_input"):
        out.append(f'<div class="muted">缺失输入：{_esc(v["missing_input"])}</div>')
    for caption, name in shots.get(claim["claim_id"], []):
        out.append(f'<figure><img src="{name}">'
                   f'<figcaption>原文对照：{_esc(caption)}　（红框为出错处）</figcaption></figure>')
    out.append('<div class="back"><a href="#toc">↑ 返回目录</a></div>')
    return "\n".join(out)


def _render_html(result: dict, shots: dict[str, list[tuple[str, str]]]) -> str:
    claims = result.get("claims") or []
    summary = result.get("summary") or {}
    company = result.get("company") or result.get("case_id") or ""
    parts: list[str] = []

    parts.append(f"""
<div class="cover">
  <h1>研报草稿核查报告</h1>
  <div class="sub">{_esc(company)}</div>
  <div class="meta">
    <div>案件编号：{_esc(result.get("case_id", ""))}</div>
    <div>运行日志编号：{_esc(result.get("run_log_id", ""))}</div>
    <div>预警截止日：{_esc(result.get("prediction_cutoff_date", ""))}</div>
    <div>主张合计 {len(claims)} 条　支持 {summary.get("SUPPORTED", 0)} 条　
         矛盾 {summary.get("CONTRADICTED", 0)} 条　证据不足 {summary.get("INSUFFICIENT", 0)} 条　
         预测/观点 {summary.get("PREDICTION", 0)} 条（后者不进核查分母）</div>
  </div>
  <div class="callout">
    本报告由确定性规则生成，数值判定不经过大模型；每条结论均可回溯到披露原文页码。<br>
    报告仅提示草稿与披露原文之间的事实性差异，不构成对任何主体的定性结论、投资建议或法律意见。
    所有条目均需由专业人员复核后使用。
  </div>
</div>
""")

    # 目录（内链）
    cov = (result.get("stages") or {}).get("stage2b_coverage") or {}
    if cov.get("detail"):
        parts.append(f'<div class="coverage-alert"><b>材料覆盖度告警</b>：'
                     f'{_esc(cov["detail"])}</div>')
    toc = ['<h2 id="toc">目录</h2>', '<ol class="toc">']
    for c in claims:
        v = c["verdict"]
        mark = "" if v["label"] == "SUPPORTED" else f'（{LABEL_CN.get(v["label"], v["label"])}）'
        toc.append(f'<li><a href="#{_esc(c["claim_id"])}">{_esc(c["claim_id"])}　'
                   f'{_esc((c.get("assertion") or c["claim_text"])[:40])}{mark}</a></li>')
    toc.append("</ol>")

    if result.get("excluded_documents"):
        toc.append('<h2>被时间过滤排除的材料</h2><table>'
                   "<tr><th>文件</th><th>发布日期</th><th>排除原因</th></tr>")
        for e in result["excluded_documents"]:
            toc.append(f'<tr><td>{_esc(e.get("file_name"))}</td>'
                       f'<td>{_esc(e.get("publication_date"))}</td>'
                       f'<td>{_esc("；".join(e.get("reasons") or []))}</td></tr>')
        toc.append("</table>")
    parts.append("\n".join(toc))

    # 逐条核查
    parts.append('<div style="page-break-before: always"></div>')
    parts.append("<h2>逐条核查</h2>")
    for c in claims:
        parts.append(_claim_html(c, shots))

    # 材料清单
    parts.append('<div style="page-break-before: always"></div>')
    parts.append("<h2>材料清单</h2><table>"
                 "<tr><th>文件</th><th>类型</th><th>发布日期</th><th>是否参与判定</th></tr>")
    seen = set()
    for c in claims:
        for e in c["verdict"].get("evidence") or []:
            key = (e.get("file_name"), e.get("source_type"))
            if key in seen:
                continue
            seen.add(key)
            parts.append(f'<tr><td>{_esc(e.get("file_name"))}</td>'
                         f'<td>{_esc(e.get("source_type"))}</td>'
                         f'<td>{_esc(e.get("publication_date"))}</td><td>参与</td></tr>')
    for e in result.get("excluded_documents") or []:
        parts.append(f'<tr><td>{_esc(e.get("file_name"))}</td><td>—</td>'
                     f'<td>{_esc(e.get("publication_date"))}</td><td>已排除</td></tr>')
    parts.append("</table>")

    if result.get("warnings"):
        parts.append("<h2>提示</h2><ul>")
        parts += [f"<li>{_esc(w)}</li>" for w in result["warnings"]]
        parts.append("</ul>")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_pdf(result: dict, out_path: str | Path, *, raw_root: str | Path | None = None,
              dpi: int = 190, max_shots_per_claim: int = 2,
              font: str | Path | None = None) -> dict[str, Any]:
    """把核查结果渲染成 PDF。返回统计信息（页数、内链条数、截屏张数）。"""
    pymupdf = _pymupdf()
    raw_root = Path(raw_root) if raw_root else ROOT / "data" / "raw"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    claims = result.get("claims") or []
    doc_paths = _doc_index(result, raw_root)

    workdir = Path(tempfile.mkdtemp(prefix="fintrace_report_"))
    try:
        font_src = Path(font) if font else next((p for p in CJK_FONT_CANDIDATES if p.exists()), None)
        if font_src and Path(font_src).exists():
            shutil.copy(font_src, workdir / "cjk.ttc")

        shots: dict[str, list[tuple[str, str]]] = {}
        shot_count = 0
        missing_locator: list[str] = []
        for c in claims:
            v = c["verdict"]
            if v["label"] != "CONTRADICTED":
                continue
            items: list[tuple[str, str]] = []
            for e in v.get("evidence") or []:
                if len(items) >= max_shots_per_claim:
                    break
                path = doc_paths.get(e.get("file_name") or "")
                if not path:
                    continue
                png = render_shot(path, e.get("page"), e.get("value"), dpi=dpi)
                if png is None:
                    continue
                name = f"ev_{c['claim_id']}_{len(items)}.png"
                (workdir / name).write_bytes(png)
                shot_count += 1
                items.append((f'{e.get("file_name")}　第 {e.get("page")} 页', name))
            if items:
                shots[c["claim_id"]] = items
            else:
                missing_locator.append(c["claim_id"])

        html = _render_html(result, shots)
        story = pymupdf.Story(html=html, archive=pymupdf.Archive(str(workdir)), user_css=CSS)
        mediabox = pymupdf.paper_rect("a4")
        where = mediabox + (42, 46, -42, -52)
        doc = story.write_with_links(lambda rect_num, filled: (mediabox, where, None))

        links = sum(len(p.get_links()) for p in doc)
        doc.subset_fonts()
        doc.save(str(out_path), garbage=4, deflate=True)
        pages = doc.page_count
        doc.close()

        return {
            "out": str(out_path).replace("\\", "/"),
            "pages": pages,
            "links": links,
            "shots": shot_count,
            "claims": len(claims),
            "contradicted": sum(1 for c in claims if c["verdict"]["label"] == "CONTRADICTED"),
            "claims_without_shot": missing_locator,
            "bytes": out_path.stat().st_size,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
