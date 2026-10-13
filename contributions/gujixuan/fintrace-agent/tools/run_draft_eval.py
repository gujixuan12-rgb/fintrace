"""跑草稿核查样本，与真值对照，输出案例级指标。

用法（仓库根目录）：

    python tools/run_draft_eval.py                       # 全部样本，规则拆解（离线可复现）
    python tools/run_draft_eval.py --llm                 # 全部样本，模型拆解
    python tools/run_draft_eval.py TXX-DRAFT-2023-001    # 只跑一个样本
    python tools/run_draft_eval.py --llm --save out/run_txx   # 同时存 payload 与报告

样本从 data/annotations/cases/*.json 自动发现。判定环节不经过模型；--llm 只影响
「草稿主张拆解」这一步，拆解失败会自动退化为规则抽取并在输出里标注。

指标口径与 draft_review.evaluate 一致：按「指标 + 数值」配对，不是按 claim_id
——模型拆解与规则拆解条数、编号都不会一致，按编号对会把拆解差异误记成判定错误。
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fintrace_engine import draft_review as dr  # noqa: E402

CASES_DIR = ROOT / "data/annotations/cases"

#: 金额/比率单位的换算表（修正值断言用：跨单位写法要能对上同一个数）
_UNIT_SCALE = {"万亿": 1e12, "亿元": 1e8, "万元": 1e4, "千元": 1e3, "元": 1.0,
               "%": 1.0, "％": 1.0, "个百分点": 1.0, "倍": 1.0, "元/股": 1.0}
_UNIT_FAMILY = {"万亿元": "金额", "亿元": "金额", "万元": "金额", "千元": "金额", "元": "金额",
                "%": "比率", "％": "比率", "个百分点": "比率", "倍": "倍数", "元/股": "每股"}
_NUM_UNIT_RX = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(万亿元|亿元|万元|千元|元/股|元|%|％|个百分点|倍)?")
_PAGE_RX = re.compile(r"第\s*\d+\s*页")


def _facts(text: str) -> set[tuple[str, float]]:
    """从一段人话里取出 (数别, 换算后的值)，用于「修正值有没有出处」的精确比对。

    没有单位的裸数字不进集合 —— 那种数字说明不了量级，不能当修正值的出处。
    """
    out: set[tuple[str, float]] = set()
    for m in _NUM_UNIT_RX.finditer(text or ""):
        unit = m.group(2)
        if not unit:
            continue
        family = _UNIT_FAMILY[unit]
        try:
            val = float(m.group(1).replace(",", "")) * _UNIT_SCALE[unit]
        except ValueError:                                  # pragma: no cover
            continue
        out.add((family, round(val, 4)))
    return out


def _same_number_other_unit(fact: tuple[str, float], allowed: set[tuple[str, float]]) -> bool:
    """同一个数字换个单位写法（「0.0718元」vs「0.0718元/股」）不算编造。

    真值正文常写「元」，引擎按指标单位写「元/股」；两个数一模一样，只是记法不同，
    不该报成「修正值无出处」——那会把真问题淹掉。金额与每股金额之间才做这种放宽。
    """
    family, value = fact
    if family not in ("金额", "每股"):
        return False
    return any(fam in ("金额", "每股") and val == value for fam, val in allowed)


class _PageReader:
    """按 (文件名, 页码) 读原 PDF 正文，带缓存 —— 审计要回查原件，不能只看输出。"""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self._docs: dict[str, object] = {}
        self._cache: dict[tuple[str, int], str] = {}

    def text(self, file_name: str, page: int | None) -> str | None:
        """返回该页正文；读不到（没这份文件/页码越界/没装 pymupdf）返回 None。"""
        path = self.paths.get(file_name)
        if path is None or page is None or not path.exists():
            return None
        key = (file_name, int(page))
        if key in self._cache:
            return self._cache[key]
        try:
            import pymupdf
        except ImportError:                                  # pragma: no cover
            return None
        doc = self._docs.get(file_name)
        if doc is None:
            try:
                doc = pymupdf.open(str(path))
            except Exception:                                # pragma: no cover - 坏文件
                return None
            self._docs[file_name] = doc
        n = int(page)
        if not (1 <= n <= doc.page_count):                   # type: ignore[attr-defined]
            self._cache[key] = ""
            return ""
        txt = doc[n - 1].get_text()                          # type: ignore[attr-defined]
        self._cache[key] = txt
        return txt


def _num_in_text(page_text: str, num: str) -> bool:
    """页面上有没有这个数 —— **直接用引擎那一份实现**（`draft_review.num_in_text`）。

    引擎不能引一个页面上指不出来的数，评测也不能用更宽的口径去「验证」它：两套实现
    迟早会对不上（18.02 命中 18.99 就是这么漏的）。所以回查口径只留一份。
    """
    return dr.num_in_text(page_text, num)


def _value_on_page(page_text: str, value: Any) -> bool:
    """这一页上有没有这个值（引擎侧的 `page_has_value`）。"""
    return dr.page_has_value(page_text, value)


def _value_strings(value: Any) -> list[str]:
    """同一个数的等价写法（引擎侧的 `value_spellings`）。"""
    return dr.value_spellings(value)


def _metric_cn(claim: dict) -> str:
    spec = dr.METRICS.get(claim.get("metric_key") or "") or {}
    return str(spec.get("cn") or "")


def _page_hint_ok(reader: "_PageReader", evidence: list[dict], claim: dict,
                  exp_item: dict) -> bool:
    """「同值多页」这个提示本身也要站得住：光数字相同不算，指标名与报告期也得对得上。

    只看数字相等就说是同值多页，会把「碰巧同值、指标完全另一回事」的取错页也放过去，
    红框就被引到错页了。这里要求实判的那一页同时含本条的指标名与报告期年份。
    """
    cn = _metric_cn(claim)
    years = set(re.findall(r"(?:19|20)\d{2}",
                           str(claim.get("source_sentence") or claim.get("claim_text") or "")
                           + " " + str(exp_item.get("claim_text") or "")))
    for e in evidence:
        txt = reader.text(str(e.get("file_name") or ""), e.get("page"))
        if not txt:
            continue
        flat = re.sub(r"\s+", "", txt)
        if cn and re.sub(r"\s+", "", cn) not in flat:
            continue
        if years and not any(y in flat for y in years):
            continue
        return True
    return False


def _quote_numbers(quote: str) -> list[str]:
    return [m.group(1) for m in _NUM_UNIT_RX.finditer(quote or "") if m.group(1)]


def audit_content(res, expected_path: Path, rows: list[dict], case: dict | None = None,
                  root: Path | None = None) -> dict:
    """标签之外的报告内容断言：错误类型、修正值出处、证据落页与**原件回查**。

    标签准确率 1.0 只说明「判对没判对」；这一层查的是判对之后报告里写的话对不对：
      · error_type 与真值是否同类（判成「矛盾」但类型说错，人工接手时会被误导）；
      · suggested_fix 里的每个金额/比率，必须能在真值给的修正值或原文引文里找到 ——
        这正是「毛利率建议改成 4,202,030,347.42%」那类错误的拦网；
      · 证据页必须落在真值给的页里（红框截的是不是那一页）；
      · 证据页上的值要**真的在原 PDF 那一页**（回查原件，不信输出里的 evidence.value：
        输出里的页号/引文被改过时，只信输出就等于自己给自己发合格证）。
    """
    exp = json.loads(expected_path.read_text(encoding="utf-8"))
    items = {c["claim_id"]: c for c in exp.get("claims", [])}
    lookup = {c["claim_id"]: c for c in res.claims}
    paths: dict[str, Path] = {}
    for d in (case or {}).get("documents") or []:
        if isinstance(d, dict) and d.get("file_name") and d.get("path"):
            p = Path(d["path"])
            paths[str(d["file_name"])] = (root / p) if root and not p.is_absolute() else p
    reader = _PageReader(paths)
    type_ok = type_bad = 0
    fix_ok = fix_bad = fix_absent = 0
    page_ok = page_bad = page_absent = 0
    pdf_ok = pdf_bad = pdf_unverified = 0
    details: list[str] = []
    for r in rows:
        exp_item = items.get(r["claim_id"])
        claim = lookup.get(r.get("matched_claim") or "")
        if not exp_item or not claim:
            continue
        verdict = claim.get("verdict") or {}
        got = verdict.get("label")
        if exp_item.get("expected_flag_as_error") and got == "CONTRADICTED":
            if (verdict.get("error_type") or "") == (exp_item.get("error_type") or ""):
                type_ok += 1
            else:
                type_bad += 1
                details.append(f"{r['claim_id']} 错误类型：真值 {exp_item.get('error_type')}"
                               f" → 实判 {verdict.get('error_type')}")
        if got != "CONTRADICTED":
            continue
        allowed = set()
        allowed |= _facts(exp_item.get("suggested_fix") or "")
        for e in exp_item.get("evidence") or []:
            allowed |= _facts(e.get("quote") or "")
        fix = str(verdict.get("suggested_fix") or "")
        got_fix = _facts(fix)
        stray = {f for f in got_fix if f not in allowed and not _same_number_other_unit(f, allowed)}
        if stray:
            fix_bad += 1
            details.append(f"{r['claim_id']} 修正值对不上真值披露：{fix[:120]}（可疑数字 "
                           f"{', '.join(f'{fam}{v:g}' for fam, v in sorted(stray))}）")
        elif got_fix:
            fix_ok += 1
        elif _PAGE_RX.search(fix) or "未绑定" in fix or "人工复核" in fix:
            fix_ok += 1      # 引用页错误只给页码；绑不到就降级不给值 —— 都属合规路径
        else:
            fix_absent += 1
            details.append(f"{r['claim_id']} 判了矛盾却没给修正值也没说降级：{fix[:120]}")
        # 回查原件：输出里的 evidence.value/页码/引文都可能被改过（内存副本演示过），
        # 只信输出就等于自己给自己发合格证 —— 这里把每条证据的数值与原 PDF 那一页对一遍。
        for e in (verdict.get("evidence") or []):
            fn = str(e.get("file_name") or "")
            pg = e.get("page")
            txt = reader.text(fn, pg)
            if txt is None:
                pdf_unverified += 1
                details.append(f"{r['claim_id']} 证据页无法回查：{fn or '（未标文件）'} 第 {pg} 页"
                               f"（文件不在本地或没装 pymupdf）")
                continue
            bad: list[str] = []
            if isinstance(e.get("value"), (int, float)) and not _value_on_page(txt, e["value"]):
                bad.append(f"值 {e['value']:g} 不在该页")
            for num in _quote_numbers(str(e.get("quote") or ""))[:3]:
                tail = str(e.get("quote") or "")
                i = tail.find(num)
                if i >= 0 and tail[i + len(num):i + len(num) + 1] in ("页", "年"):
                    continue                       # 引文里的页码/年份不算披露值
                if not _num_in_text(txt, num):
                    bad.append(f"引文里的数 {num} 不在该页")
                    break
            if bad:
                pdf_bad += 1
                details.append(f"{r['claim_id']} 证据页回查不过：{fn} 第 {pg} 页 —— {'；'.join(bad)}")
            else:
                pdf_ok += 1
        got_pages = {e.get("page") for e in (verdict.get("evidence") or []) if e.get("page")}
        exp_pages = {e.get("pdf_page") for e in (exp_item.get("evidence") or []) if e.get("pdf_page")}
        if not got_pages:
            page_absent += 1
            details.append(f"{r['claim_id']} 判了矛盾却没有证据页（红框无从落）")
        elif exp_pages and got_pages & exp_pages:
            page_ok += 1
        else:
            # 没有交集不一定错：同一披露常在多页出现（基本每股收益在第 7 页摘要表和第
            # 237 页每股收益表都有）。看实判那几页的证据值里有没有本条的应取值 ——
            # 有就是「同值多页」，没有才可能是取错页；两种都列出来给人确认红框。
            want = _facts(f"{claim.get('claim_value')} {claim.get('claim_unit') or ''}")
            want |= {f for f in _facts(str(verdict.get("suggested_fix") or ""))}
            got_vals = {(fam, round(float(e["value"]), 4))
                        for e in (verdict.get("evidence") or [])
                        if isinstance(e.get("value"), (int, float)) for fam in ("金额", "比率",
                                                                                "每股", "倍数")}
            if want & got_vals and _page_hint_ok(reader, verdict.get("evidence") or [], claim, exp_item):
                page_ok += 1
                details.append(f"{r['claim_id']} 证据落页与真值不同（真值 {sorted(exp_pages)} → "
                               f"实判 {sorted(got_pages)}），实判页确有该披露值且指标、报告期都对得上，"
                               f"属同值多页，红框落哪一页需人工定")
            elif want & got_vals:
                page_bad += 1
                details.append(f"{r['claim_id']} 证据落页与真值不同且只是数字相同：真值 "
                               f"{sorted(exp_pages)} → 实判 {sorted(got_pages)}，实判页找不到本条的"
                               f"指标名或报告期，不能算同值多页")
            else:
                page_bad += 1
                details.append(f"{r['claim_id']} 证据落页与真值不同：真值 {sorted(exp_pages)} → "
                               f"实判 {sorted(got_pages)}，且实判页的证据值对不上本条应取值")
    return {"type_ok": type_ok, "type_bad": type_bad, "fix_ok": fix_ok, "fix_bad": fix_bad,
            "fix_absent": fix_absent, "page_ok": page_ok, "page_bad": page_bad,
            "page_absent": page_absent, "pdf_ok": pdf_ok, "pdf_bad": pdf_bad,
            "pdf_unverified": pdf_unverified, "details": details}


def run_one(case_path: Path, *, use_llm: bool, save_dir: Path | None = None) -> dict:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    case_id = case.get("case_id", case_path.stem)
    print("=" * 78)
    print(f"样本 {case_id}  ←  {case_path.relative_to(ROOT)}")
    print(f"拆解方式: {'模型（失败则退化为规则）' if use_llm else '规则（离线）'}")

    res = dr.run_draft_case(case, use_llm=use_llm)
    for w in res.warnings:
        print(f"  提醒: {w}")
    print(f"  材料: 纳入 {len(res.stages['stage2_materials']['allowed'])} 份，"
          f"截止日排除 {len(res.stages['stage2_materials']['excluded'])} 份"
          f"（{', '.join(res.stages['stage2_materials']['excluded']) or '无'}）")
    print(f"  拆解: {res.stages['stage1_decomposition']['method']} "
          f"→ {res.stages['stage1_decomposition']['count']} 条主张")

    metrics = dr.evaluate(res, ROOT / case["expected"])

    lookup = {c["claim_id"]: c for c in res.claims}
    print(f"  {'真值条目':<12} {'真值标签':<13} {'实判标签':<13} {'一致':<5} "
          f"{'真值错误类型':<18} {'实判错误类型':<18} 实判主张")
    for r in metrics["rows"]:
        c = lookup.get(r["matched_claim"])
        ctext = (c.get("claim_text") or "")[:34].replace("\n", " ") if c else "（未匹配到）"
        print(f"  {r['claim_id']:<12} {r['expected'] or '—':<13} {r['got'] or '（无）':<13} "
              f"{'是' if r['match'] else '否':<5} "
              f"{r['expected_error_type'] or '—':<18} {r['got_error_type'] or '—':<18} {ctext}")

    ed = metrics["error_detection"]
    sec, ex = metrics["sections"], metrics["extra_outputs"]
    print(f"  指标: 覆盖 {metrics['coverage']}  "
          f"标签准确率 {metrics['label_accuracy']}  "
          f"TP {ed['tp']} / FP {ed['fp']} / FN {ed['fn']} / TN {ed['tn']}  "
          f"精确率 {ed['precision']}  召回 {ed['recall']}  "
          f"误报率 {ed['false_positive_rate']}  "
          f"页码定位 {metrics['page_localization_accuracy']}")
    print(f"  分项: 抽取覆盖 {sec['抽取覆盖']['匹配上']}/{sec['抽取覆盖']['金标条数']}"
          f"（运行时 {sec['抽取覆盖']['运行时条数']} 条）  "
          f"额外输出 {ex['count']} 条（报成矛盾 {ex['as_error']}：计入 FP {ex.get('as_fp', 0)}"
          f" / 待审 {ex.get('needs_review', 0)}）  "
          f"错误类型 {sec['错误类型']['一致']}/{sec['错误类型']['应报错误条数']}  "
          f"证据定位 {sec['证据定位']['命中']}/{sec['证据定位']['可核条数']}"
          f"  金标{'完整' if metrics.get('gold_complete') else '未声明完整'}")
    for it in ex["items"]:
        print(f"    额外输出：{it['claim_id']} [{it['label']}] {it['metric_key']} "
              f"{it['claim_text']}")

    metrics["content"] = audit_content(res, ROOT / case["expected"], metrics["rows"], case,
                                       ROOT)
    ct = metrics["content"]
    print(f"  内容断言（标签之外）: 错误类型一致 {ct['type_ok']}/"
          f"{ct['type_ok'] + ct['type_bad']}  "
          f"修正值有出处 {ct['fix_ok']}/{ct['fix_ok'] + ct['fix_bad'] + ct['fix_absent']}"
          f"（无出处 {ct['fix_bad']} / 没给值 {ct['fix_absent']}）  "
          f"证据落页命中 {ct['page_ok']}/{ct['page_ok'] + ct['page_bad'] + ct['page_absent']}"
          f"（落错页 {ct['page_bad']} / 无证据页 {ct['page_absent']}）  "
          f"原件回查 {ct['pdf_ok']}/{ct['pdf_ok'] + ct['pdf_bad'] + ct['pdf_unverified']}"
          f"（对不上 {ct['pdf_bad']} / 无法回查 {ct['pdf_unverified']}）")
    if ct["details"]:
        print(f"  -- 内容断言明细（{len(ct['details'])} 条）--")
        for d in ct["details"]:
            print(f"  {d}")

    misses = [r for r in metrics["rows"] if not r["match"]]
    if misses:
        print(f"  -- 不一致明细（{len(misses)} 条）--")
        for r in misses:
            c = lookup.get(r["matched_claim"])
            if c:
                v = c["verdict"]
                print(f"  {r['claim_id']}: 真值 {r['expected']}"
                      f"{'/' + r['expected_error_type'] if r['expected_error_type'] else ''}"
                      f" → 实判 {r['got']}"
                      f"{'/' + r['got_error_type'] if r['got_error_type'] else ''}")
                print(f"      主张原文: {c.get('claim_text')}")
                print(f"      判定依据: {str(v.get('basis'))[:200]}")
            else:
                print(f"  {r['claim_id']}: 真值 {r['expected']} → 压根没拆出这条主张")

    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / "payload.json").write_text(
            json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (save_dir / "报告.md").write_text(res.markdown, encoding="utf-8")
        (save_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已存: {save_dir}/payload.json、报告.md、metrics.json")

    return metrics


def verdict_of(agg: dict, extra: dict) -> tuple[int, str]:
    """由「内容断言聚合 + 额外输出统计」给出退出码与结论话（纯函数，回归测试直接调它）。

    通过的条件是：内容断言零不过，**且**没有任何未处置的「多报错误裁决」。
    额外报成矛盾的主张：金标声明完整时已计入 FP，未声明完整时列为待审 —— 两种都不算通过。
    旧口径只看内容断言，于是「多报一条错误裁决」可以零成本通过（P1-10 探针实测：
    注入一条 as_error=1，仍打印「两层都过了」并返回 0）。
    """
    fails = (agg["type_bad"] + agg["fix_bad"] + agg["fix_absent"] + agg["page_bad"]
             + agg["page_absent"] + agg["pdf_bad"] + agg["pdf_unverified"])
    if fails:
        return 1, (f"！！ 内容断言有 {fails} 处不过 —— 标签指标只说明「判对没判对」，"
                   f"内容不过就不算通过（退出码 1）。")
    if extra.get("fp"):
        return 1, (f"！！ 有 {extra['fp']} 条多报的错误裁决（已计入 FP）—— 不算通过。")
    if extra.get("review"):
        return 1, (f"！！ 有 {extra['review']} 条多报的错误裁决待审：样本 "
                   f"{'、'.join(extra.get('incomplete') or [])} 的金标未声明完整"
                   f"（annotation_scope != exhaustive），既不能算 FP 也不能算通过，需人工判。")
    if extra.get("incomplete"):
        return 0, ("内容断言全部通过（标签与内容两层都过了）。"
                   f"注意：样本 {'、'.join(extra['incomplete'])} 的金标未声明完整，"
                   "「无误报」这一结论不得外推。")
    return 0, "内容断言全部通过（标签与内容两层都过了）。"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="跑草稿核查样本并对照真值")
    ap.add_argument("case_ids", nargs="*", help="样本 id；省略则跑全部")
    ap.add_argument("--llm", action="store_true", help="用模型拆解主张（默认规则）")
    ap.add_argument("--save", metavar="DIR", help="把结果写到该目录")
    args = ap.parse_args(argv[1:])

    cases = sorted(CASES_DIR.glob("*.json"))
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [p for p in cases if p.stem in wanted or
                 json.loads(p.read_text(encoding="utf-8")).get("case_id") in wanted]
    if not cases:
        print("!! 没有匹配的样本")
        return 2

    all_metrics = []
    for p in cases:
        save = Path(args.save) / p.stem if args.save else None
        all_metrics.append(run_one(p, use_llm=args.llm, save_dir=save))

    print("=" * 78)
    tp = sum(m["error_detection"]["tp"] for m in all_metrics)
    fp = sum(m["error_detection"]["fp"] for m in all_metrics)
    fn = sum(m["error_detection"]["fn"] for m in all_metrics)
    gold = sum(m["gold_claims"] for m in all_metrics)
    matched = sum(m["matched_claims"] for m in all_metrics)
    hit = sum(1 for m in all_metrics for r in m["rows"] if r["match"])
    print(f"汇总（{len(all_metrics)} 个样本，案例级，不得外推为泛化准确率）:")
    print(f"  真值主张 {gold} 条，匹配上 {matched} 条（覆盖 {matched / gold:.4f}）；"
          f"标签一致 {hit}/{gold}（{hit / gold:.4f}）")
    print(f"  错误识别 TP {tp} / FP {fp} / FN {fn}"
          f"（精确率 {tp / (tp + fp) if tp + fp else 0:.4f}，"
          f"召回 {tp / (tp + fn) if tp + fn else 0:.4f}）")
    extra = sum(m["extra_outputs"]["count"] for m in all_metrics)
    extra_err = sum(m["extra_outputs"]["as_error"] for m in all_metrics)
    extra_fp = sum(m["extra_outputs"].get("as_fp", 0) for m in all_metrics)
    extra_review = sum(m["extra_outputs"].get("needs_review", 0) for m in all_metrics)
    incomplete = [m["case_id"] for m in all_metrics if not m.get("gold_complete")]
    print(f"  分项汇总: 运行时多报主张 {extra} 条，其中报成「矛盾」{extra_err} 条"
          f"（计入 FP {extra_fp} / 待审 {extra_review}）")
    if incomplete:
        print(f"    金标未声明完整：{'、'.join(incomplete)}"
              f"（多报的错误裁决按「待审」处理，「无误报」不得外推）")
    keys = ("type_ok", "type_bad", "fix_ok", "fix_bad", "fix_absent",
            "page_ok", "page_bad", "page_absent", "pdf_ok", "pdf_bad", "pdf_unverified")
    agg = {k: sum(m["content"][k] for m in all_metrics) for k in keys}
    print(f"  内容断言（标签之外，全量）: 错误类型一致 {agg['type_ok']}/"
          f"{agg['type_ok'] + agg['type_bad']}  "
          f"修正值有出处 {agg['fix_ok']}/"
          f"{agg['fix_ok'] + agg['fix_bad'] + agg['fix_absent']}"
          f"（无出处 {agg['fix_bad']} / 没给值 {agg['fix_absent']}）  "
          f"证据落页命中 {agg['page_ok']}/"
          f"{agg['page_ok'] + agg['page_bad'] + agg['page_absent']}"
          f"（落错页 {agg['page_bad']} / 无证据页 {agg['page_absent']}）  "
          f"原件回查 {agg['pdf_ok']}/"
          f"{agg['pdf_ok'] + agg['pdf_bad'] + agg['pdf_unverified']}"
          f"（对不上 {agg['pdf_bad']} / 无法回查 {agg['pdf_unverified']}）")
    code, why = verdict_of(agg, {"fp": extra_fp, "review": extra_review,
                                 "incomplete": incomplete})
    if why:
        print(why)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
