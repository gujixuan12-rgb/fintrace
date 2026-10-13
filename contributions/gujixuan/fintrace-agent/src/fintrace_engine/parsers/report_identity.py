"""从材料 PDF 里认出「这是谁家的、哪一期、什么时候发的」。

导入页面的目标是**不用手填**：传上来一份年报，公司名、股票代码、报告期、披露日、
来源类型都应当由程序自己看出来，并且把依据（原文里那一行）一并返回，方便人核对。

只认**字面写出来的信息**，不做任何推测性补全：
- 认出什么就返回什么，认不出的字段留空，由调用方决定要不要人确认；
- 返回 `evidence` 说明每个字段是从哪句话里读出来的，页面可以直接展示；
- 带 `assumed` 标记的字段表示「按规则推的、不是原文写的」（例如封面只写了「2024 年 4 月」，
  日期取该月最后一天），页面上必须显示成推定值。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .document_recognizer import classify

#: 材料类型：文件名优先，文件名认不出再看正文。
_CONTENT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"问询函|关注函"), "inquiry_letter_reply"),
    (re.compile(r"更正公告|差错更正|更正及定期报告|澄清公告"), "correction_announcement"),
    (re.compile(r"审计报告"), "audit_report"),
    (re.compile(r"年度报告|年报"), "original_annual_report"),
    (re.compile(r"半年度报告|季度报告|半年报|季报"), "original_interim_report"),
    (re.compile(r"研报|研究报告|深度报告|评级报告"), "research_report"),
]

#: 「公司代码：600556」「证券代码：600107」「股票代码：600107」
_CODE_RX = re.compile(r"(?:公司|证券|股票)代码\s*[：:]\s*(\d{6})")
#: 「公司简称：天下秀」「股票简称：美尔雅」「证券简称：X」
_SHORT_RX = re.compile(r"(?:公司|证券|股票)简称\s*[：:]\s*([^\s，,；;、]{2,12})")
#: 封面里的公司全称（一行只写公司名，以组织形式结尾）。
#: 字符类里绝不能放 \s：那会让 `^...$` 跨行把好几行拼成一个「名字」。
_NAME_RX = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9（）()·&\-]{4,40}?"
    r"(?:股份有限公司|有限责任公司|有限公司|集团公司|集团)\s*$", re.M
)
#: 兜底：正文里嵌在句子中的公司全称，取最长的一个
_NAME_INLINE_RX = re.compile(r"[\u4e00-\u9fa5（）()·]{4,30}?(?:股份有限公司|有限责任公司|有限公司)")
#: 没有任何「代码：」标签时（北交所封面是散排文本框，代码单独一行）认这一种
_CODE_ALONE_RX = re.compile(r"^\s*(\d{6})\s*$", re.M)
#: 报告期：「2023 年年度报告」「2023年半年度报告」「2023 年第三季度报告」
_PERIOD_RX = re.compile(r"(20\d{2})\s*年\s*(年度报告|半年度报告|第[一二三四1-4]季度报告|季度报告)")
#: 公告标题里引用的报告期：「关于《2023 年年度报告》的更正公告」
_QUOTED_PERIOD_RX = re.compile(r"[《【](20\d{2})\s*年\s*(年度报告|半年度报告)")

_DATE_ARABIC_RX = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(text: str) -> int | None:
    """把「二〇二四」「二00八」「十」「二十三」这类写法转成整数；转不了返回 None。

    允许混排与空格：真实材料里出现过「二00 八年六月三日」（PDF 字距被拆开）。
    """
    s = re.sub(r"\s+", "", text or "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" not in s:
        digits = []
        for ch in s:
            if ch.isdigit():
                digits.append(ch)
            elif ch in _CN_DIGITS:
                digits.append(str(_CN_DIGITS[ch]))
            else:
                return None
        return int("".join(digits)) if digits else None
    head, _, tail = s.partition("十")
    tens = _cn_number(head) if head else 1
    ones = _cn_number(tail) if tail else 0
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


_CN_NUM = r"[〇零一二三四五六七八九0-9十]"


def _cn_date(text: str) -> tuple[str, bool] | None:
    """中文日期 → (ISO 日期, 是否是推定值)；只有年月时取该月最后一天。

    必须含至少一个中文数字：正文里的「2023年12月31日」是阿拉伯数字写的，
    那是报告期末，不能让它走到这条分支上来。
    """
    m = re.search(rf"({_CN_NUM}{{2,5}})\s*年\s*({_CN_NUM}{{1,3}})\s*月"
                  rf"(?:\s*({_CN_NUM}{{1,3}})\s*日)?", text)
    if not m or not re.search(r"[〇零一二三四五六七八九]", m.group(0)):
        return None
    y, mo, d = (_cn_number(g) for g in m.groups())
    if not y or not mo:
        return None
    try:
        if d:
            return date(y, mo, d).isoformat(), False
        return _month_end(y, mo), True
    except ValueError:
        return None


def _period_of(text: str) -> tuple[str, str]:
    """从标题/正文里读报告期，返回 (period, 原文依据)。"""
    m = _QUOTED_PERIOD_RX.search(text) or _PERIOD_RX.search(text)
    if not m:
        return "", ""
    year, kind = m.group(1), m.group(2)
    if "半年度" in kind:
        return f"{year}H1", m.group(0)
    if "季度" in kind:
        mark = re.search(r"第([一二三四1-4])季度", kind)
        table = {"一": "1", "二": "2", "三": "3", "四": "4"}
        q = table.get(mark.group(1), "") if mark else ""
        return f"{year}Q{q}" if q else f"{year}Q", m.group(0)
    return f"{year}A", m.group(0)


def _month_end(y: int, mo: int) -> str:
    nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1).isoformat()


def _cover_dates(page: str) -> list[tuple[str, str, bool]]:
    """从封面/公告页里找「这份材料是哪天发的」。

    只认两种位置，别的地方一律不看：**整行只有日期**（封面底部常见「二〇二四年四月」），
    或者**页面最后几行**（公告落款日期）。满页乱抓会把正文里的「截至 2023 年 12 月 31 日」
    当披露日——那是报告期末，不是披露日，实测踩过。
    """
    if not page:
        return []
    lines = [ln.strip() for ln in page.splitlines()]
    out: list[tuple[str, str, bool]] = []
    tail_from = max(0, len(lines) - 6)
    for i, ln in enumerate(lines):
        if not ln:
            continue
        flat = re.sub(r"\s+", "", ln)  # PDF 的字距会把「二00 八年」拆开，先去空格再认
        standalone = i >= tail_from or len(re.sub(r"[年月日]", "", flat)) <= 9
        if not standalone:
            continue
        m = _DATE_ARABIC_RX.fullmatch(flat.strip("　()（）"))
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
            try:
                if d:
                    out.append((date(y, mo, int(d)).isoformat(), f"封面单独的日期行「{ln}」", False))
                else:
                    out.append((_month_end(y, mo), f"封面「{ln}」（只写到月，取该月最后一天）", True))
            except ValueError:
                pass
            continue
        got = _cn_date(flat)
        if got:
            iso, is_guess = got
            why = f"封面/落款的中文日期「{ln.strip()[:24]}」"
            if is_guess:
                why += "（只写到月，取该月最后一天）"
            out.append((iso, why, is_guess))
    return out


def _publication_date(file_name: str, cover: str, tail: str) -> tuple[str, str, bool]:
    """披露日：文件名里的日期 > 封面/落款的日期。都认不出就留空，由调用方去问人。

    返回 (日期, 依据, 是否是推定值)。只写到「年月」时取该月最后一天，算推定。
    """
    m = re.match(r"\s*(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", file_name or "")
    if m:
        try:
            return date(*(int(g) for g in m.groups())).isoformat(), f"文件名里的日期 {m.group(0)}", False
        except ValueError:
            pass

    cands = _cover_dates(cover)
    if not cands and tail:
        cands = _cover_dates(tail)
    if cands:
        # 多条时取最晚的：封面底部那行才是定稿日期
        return max(cands, key=lambda c: c[0])
    return "", "", False


def detect_document(pages: list[str], file_name: str = "") -> dict:
    """认一份材料。`pages` 是 PDF 的文本页（按页给出即可，只用到头两页和末页）。"""
    head = "\n".join(pages[:2])
    tail = pages[-1] if pages else ""
    evidence: dict[str, str] = {}

    code, code_assumed = "", False
    m = _CODE_RX.search(head) or _CODE_RX.search(tail)
    if m:
        code, evidence["stock_code"] = m.group(1), m.group(0).strip()
    else:
        # 没有标签时只认「整行就是一个 6 位数字」（北交所封面的排法），并标成推定值
        alone = {mm.group(1) for mm in _CODE_ALONE_RX.finditer(pages[0] if pages else "")}
        if len(alone) == 1:
            code, code_assumed = alone.pop(), True
            evidence["stock_code"] = "首页上单独一行的 6 位数字（页面没有写「代码：」标签）"

    short_name = ""
    m = _SHORT_RX.search(head)
    if m:
        short_name, evidence["short_name"] = m.group(1), m.group(0).strip()

    # 注意别复用 basis 这个变量名：下面算报告期时也会用它，会把这里的结果覆盖掉
    company, name_basis = "", ""
    m = _NAME_RX.search(head)
    if m:
        company, name_basis = m.group(0).strip(), "封面上的公司全称"
    else:
        found = sorted(_NAME_INLINE_RX.findall(head), key=len)
        if found and len(found[-1]) >= 6:
            company, name_basis = found[-1], "正文里出现的公司全称"
        elif short_name:
            company, name_basis = short_name, "材料正文没有写全称，用的是证券简称"
    if company:
        evidence["company"] = name_basis

    period, basis = _period_of(file_name + "\n" + head)
    period_assumed = False
    if not period:
        # 散排封面（北交所常见）：年份和标题各自是独立的文本框，连不成一句话。
        # 只在「首页恰好一个年份 + 一个报告类型」时认，并标成推定。
        years = set(re.findall(r"^\s*(20\d{2})\s*$", head, re.M))
        kind_line = re.search(r"^\s*(年度报告摘要|年度报告|半年度报告|第一季度报告|第三季度报告)\s*$", head, re.M)
        if len(years) == 1 and kind_line:
            kind = kind_line.group(1)
            period = years.pop() + ("H1" if "半年度" in kind else "A")
            period_assumed = True
            evidence["period"] = f"首页单独一行的年份 + 「{kind}」"
    elif period:
        evidence["period"] = basis

    kind = classify(file_name)
    if kind == "announcement":  # 文件名没给出线索，看正文
        for rx, k in _CONTENT_RULES:
            if rx.search(head):
                kind = k
                break

    pub, why, assumed = _publication_date(file_name, pages[0] if pages else "", tail)
    if pub:
        evidence["publication_date"] = why

    return {
        "company": company,
        "company_basis": name_basis,
        "short_name": short_name,
        "stock_code": code,
        "stock_code_assumed": code_assumed,
        "period": period,
        "period_assumed": period_assumed,
        "publication_date": pub,
        "publication_date_assumed": assumed,
        "source_type": kind,
        "source_type_basis": "文件名" if classify(file_name) != "announcement" else "正文",
        "evidence": evidence,
    }


def from_index(title: str, code: str = "", short_name: str = "",
               published_at: str = "") -> dict:
    """用公告索引里的元数据填字段——还没下载就能把表填好。

    索引里的东西是源端给的（标题、证券代码、证券简称、公告日期），属于原文事实，
    不需要标成推定；但「报告期」是从标题里读出来的，依据一并给出。
    """
    period, basis = _period_of(title)
    kind = classify(title)
    evidence = {"publication_date": "巨潮公告索引里的公告日期"}
    if code:
        evidence["stock_code"] = f"公告索引里的证券代码：{code}"
    if short_name:
        evidence["company"] = f"公告索引里的证券简称：{short_name}"
    if period:
        evidence["period"] = f"公告标题「{basis}」"
    return {
        "company": short_name,
        "company_basis": "公告索引里的证券简称",
        "short_name": short_name,
        "stock_code": code,
        "stock_code_assumed": False,
        "period": period,
        "period_assumed": False,
        "publication_date": published_at,
        "publication_date_assumed": False,
        "source_type": kind,
        "source_type_basis": "公告标题",
        "evidence": evidence,
    }


def detect_pdf(blob: bytes, file_name: str = "") -> dict:
    """从 PDF 字节里认字段。需要 pymupdf（可选依赖 `.[pdf]`）。"""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise RuntimeError("需要 pymupdf 才能自动识别 PDF，请安装 `.[pdf]`") from exc

    doc = pymupdf.open(stream=blob, filetype="pdf")
    try:
        pages = [doc[i].get_text() for i in range(min(2, doc.page_count))]
        if doc.page_count > 2:
            pages.append(doc[doc.page_count - 1].get_text())
        title = (doc.metadata or {}).get("title") or ""
    finally:
        doc.close()
    out = detect_document(pages, file_name)
    if not out["company"] and title:
        out["title_hint"] = title
    return out


def detect_file(path: str | Path) -> dict:
    """认本地文件（按文件名 + 内容）。"""
    p = Path(path)
    return detect_pdf(p.read_bytes(), p.name)
