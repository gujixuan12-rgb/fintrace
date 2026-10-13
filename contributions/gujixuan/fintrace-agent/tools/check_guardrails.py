"""反例回归：本项目最容易被「看起来通过了」骗过去的几处，一次性跑完。

用法（仓库根目录）：

    python tools/check_guardrails.py          # 全跑，有不过就退出码 1

六节：
  ① 解释守门人——换单位、翻符号、编数字、**拿草稿的错值当事实**、**页码与数值分开绑**；
  ② 内容审计——篡改内存里的证据页/引文后，审计必须报不过（不信输出里的 evidence）；
  ③ 断言抽取——两年同值、事实+预测同句、半年+全年同句、归母+扣非同句、水平值+同比；
  ④ 草稿错值伪装成材料披露 / 同句引用互相掩护；
  ⑤ 原件回查的数值匹配（带符号、要数字边界、按精度）；
  ⑥ 缓存按输入失效（结果 / 截图 / 报告 PDF 三类共用一个输入指纹）与「多报的错误裁决」进验收。

每节都印「旧行为」与「新行为」对比（旧行为在脚本里现场模拟，改坏了立刻能看出来）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from fintrace_engine import draft_review as dr                      # noqa: E402
from fintrace_engine.verification import explanation_guard as eg     # noqa: E402

TXX_CASE = ROOT / "data/annotations/cases/tianxiaxiu_2023_draft_case.json"
FAILS: list[str] = []


def _ok(name: str, cond: bool, note: str = "") -> None:
    print(f"  {'通过' if cond else '不过'}　{name}{('：' + note) if note else ''}")
    if not cond:
        FAILS.append(name)


def _legacy_pool(blob: str) -> "eg.Corpus":
    """旧行为：不做来源隔离 —— 草稿自己写的值也当事实（这正是「草稿错值被当事实」的成因）。"""
    corpus = eg._facts_from_result(blob)
    for f in corpus.claim:
        corpus.material.append(replace(f, origin=eg.MATERIAL))
    return corpus


def _check_guard(text: str, blob: str) -> list[str]:
    return eg.check(text, result_blob=blob)


def _legacy_citations(text: str, corpus: "eg.Corpus") -> list[str]:
    """旧引用校验：只查「页码是否属于某个文件的证据页集合」，不与同句数值联合绑定。

    这正是「毛利率应为 18.02%，见第 8 页」能过关的原因：第 8 页确实是别的证据页。
    """
    problems = []
    for pm in eg._PAGE_RX.finditer(text):
        page = int(pm.group(1))
        if corpus.pages and not any(page in s for s in corpus.pages.values()):
            problems.append(f"引用页码对不上：材料里没有第 {page} 页")
    return problems


def _check_guard_legacy(text: str, blob: str) -> list[str]:
    """旧行为模拟：只换事实池（草稿值也算事实），其余跳过规则与真实 check 一致。"""
    corpus = _legacy_pool(blob)
    spans = [(m.start(), m.end()) for m in eg._FILE_RX.finditer(text)]
    problems = []
    for m in eg._mentions(text):
        if m.unit == "页" or any(a <= m.start < b for a, b in spans):
            continue
        note = eg._match_mention(m, corpus, text, 0.0)
        if note:
            problems.append(note)
    return problems + _legacy_citations(text, corpus)


def section1() -> None:
    print("① 解释守门人")
    case = json.loads(TXX_CASE.read_text(encoding="utf-8"))
    res = dr.run_draft_case(case, use_llm=False)
    blob = json.dumps(res.to_dict(), ensure_ascii=False)

    # 找一条「草稿值错、材料值对」的主张（毛利率 21.75% vs 18.02%）
    bad = next(c for c in res.claims if c["metric_key"] == "gross_margin")
    wrong = f"{bad['claim_value']:g}"
    good = "18.02"
    page_with_value = next(e["page"] for e in bad["verdict"]["evidence"] if e.get("page"))
    other_page = next(e["page"] for c in res.claims for e in c["verdict"]["evidence"]
                      if e.get("page") and e["page"] != page_with_value)

    cases = [
        ("换单位（亿元→万元）", "营业收入 42.02 万元。", False),
        ("单位正确（亿元）", "营业收入 42.02 亿元。", True),
        ("翻符号（下降→增长）", "归母净利润 0.93 亿元，同比增长 48.43%。", False),
        ("凭空写毛利率", "2023 年公司毛利率 7%。", False),
        ("草稿的错值当事实", f"毛利率修正值为 {wrong}%。", False),
        ("引述草稿时可以提草稿值", f"草稿写的是 {wrong}%，材料披露 {good}%。", True),
        ("页码与数值对不上", f"毛利率应为 {good}%，见第 {other_page} 页。", False),
        ("页码与数值对得上", f"毛利率应为 {good}%，见第 {page_with_value} 页。", True),
    ]
    for name, text, should_pass in cases:
        legacy = _check_guard_legacy(text, blob)
        now = _check_guard(text, blob)
        verdict = (len(now) == 0) if should_pass else bool(now)
        old_word = "放过" if not legacy else "拦下"
        new_word = "放过" if not now else "拦下"
        _ok(f"{name}（旧：{old_word} → 新：{new_word}）", verdict,
            "" if verdict else f"新行为给的是 {now}")


def section2() -> None:
    print("② 内容审计：篡改输出后必须报不过")
    import run_draft_eval as rde

    case = json.loads(TXX_CASE.read_text(encoding="utf-8"))
    res = dr.run_draft_case(case, use_llm=False)
    rows = dr.evaluate(res, ROOT / case["expected"])["rows"]

    clean = rde.audit_content(res, ROOT / case["expected"], rows, case, ROOT)
    _ok("干净输出：原件回查全过", clean["pdf_bad"] == 0 and clean["pdf_unverified"] == 0,
        f"对不上 {clean['pdf_bad']} / 无法回查 {clean['pdf_unverified']}")

    # 只在内存副本里把证据页改成第 1 页并清空引文（原 PDF 第 1 页没有那个值）
    tally = 0
    for c in res.claims:
        for e in (c["verdict"].get("evidence") or []):
            e["page"], e["quote"] = 1, ""
            tally += 1
    tampered = rde.audit_content(res, ROOT / case["expected"], rows, case, ROOT)
    _ok("篡改证据页后：审计报不过", tampered["pdf_bad"] > 0,
        f"回查对不上 {tampered['pdf_bad']} 条（共 {tally} 条证据）")

    # 再把证据值挪一点点（18.02 → 18.03）：旧匹配把两边都碾成数字汤、还带取整候选，
    # 18.03 的「18」会命中页面上的 18.02，照样报过（实测绕过）
    res2 = dr.run_draft_case(case, use_llm=False)
    rows2 = dr.evaluate(res2, ROOT / case["expected"])["rows"]
    moved = 0
    for c in res2.claims:
        for e in (c["verdict"].get("evidence") or []):
            if isinstance(e.get("value"), float) and e["value"] == 18.02:
                e["value"] = 18.03
                moved += 1
    near = rde.audit_content(res2, ROOT / case["expected"], rows2, case, ROOT)
    _ok("把证据值 18.02 改成 18.03 后：审计报不过", moved > 0 and near["pdf_bad"] > 0,
        f"改了 {moved} 条，回查对不上 {near['pdf_bad']} 条")


def section3() -> None:
    print("③ 断言抽取与去重")
    cases = [
        ("两年同值要留两条", "2023 年营业收入 42.02 亿元，2022 年营业收入 42.02 亿元。", 2),
        ("事实与预测同句拆两条", "公司 2023 年实现营业收入 42.02 亿元，预计 2024 年营业收入 45.20 亿元。", 2),
        ("半年与全年同句拆两条", "2026 年上半年营业收入 12.43 亿元，2026 年全年营业收入 30.00 亿元。", 2),
        ("归母与扣非同句拆两条", "归母净利润 0.93 亿元，扣非净利润 0.74 亿元。", 2),
        ("水平值与同比只算一条", "销售费用本期发生额 25,097.45 万元，同比下降 21.09%。", 1),
    ]
    for name, text, want in cases:
        claims = dr.extract_claims_rules(text)
        _ok(f"{name}（要 {want} 条，实得 {len(claims)} 条）", len(claims) == want,
            "" if len(claims) == want else "；".join(c["claim_text"] for c in claims))

    # 混合句里的事实子句不能被预测子句连坐（实际值被归为「预测」就是这么来的）
    text = "公司 2023 年实现营业收入 42.02 亿元，预计 2024 年营业收入 45.20 亿元。"
    claims = [dr._to_claim(c, i) for i, c in enumerate(dr.extract_claims_rules(text), 1)]
    fact = next(c for c in claims if "2024" not in c.claim_text)
    fc = next(c for c in claims if "预计" in c.claim_text)
    _ok("事实子句不算预测", dr._is_forecast_text(fact) is None,
        f"判定为 {dr._is_forecast_text(fact)}")
    _ok("预测子句算预测", dr._is_forecast_text(fc) == "预计")


def section4() -> None:
    print("④ 反例：草稿错值伪装成材料披露 / 同句引用互相掩护")
    case = json.loads(TXX_CASE.read_text(encoding="utf-8"))
    res = dr.run_draft_case(case, use_llm=False)
    blob = json.dumps(res.to_dict(), ensure_ascii=False)
    bad = next(c for c in res.claims if c["metric_key"] == "gross_margin")
    wrong = f"{bad['claim_value']:g}"

    cases = [
        ("「年报给出毛利率 X%」不算引述草稿", f"年报给出毛利率 {wrong}%。", False),
        ("「原文写的是 X%」也不算", f"原文写的是 {wrong}%。", False),
        ("点明草稿才允许转述", f"草稿写的是 {wrong}%，材料披露 18.02%。", True),
        ("引述标记不能跨子句连坐", f"草稿写的是营业收入 42.02 亿元，毛利率 {wrong}% 也一并核过。",
         False),
        ("同句正确引用不能掩护错误引用",
         "营业收入 42.02 亿元见第 8 页，另外，我们核查发现毛利率 18.02% 也见第 8 页。", False),
        ("两个引用各自都成立就放行",
         "营业收入 42.02 亿元见第 8 页，毛利率 18.02% 见第 23 页。", True),
    ]
    for name, text, should_pass in cases:
        legacy = _check_guard_legacy(text, blob)
        now = _check_guard(text, blob)
        verdict = (len(now) == 0) if should_pass else bool(now)
        old_word = "放过" if not legacy else "拦下"
        new_word = "放过" if not now else "拦下"
        _ok(f"{name}（旧：{old_word} → 新：{new_word}）", verdict,
            "" if verdict else f"新行为给的是 {now}")


def section5() -> None:
    print("⑤ 反例：原件回查的数值匹配（带符号、要数字边界、按精度）")
    import run_draft_eval as rde

    cases = [
        ("18.02 不该命中 18.99", "毛利率 18.99", 18.02, False),
        ("18.02 不该命中 118.02", "合计 118.02", 18.02, False),
        ("正数不该命中负数", "同比增减 48.43", -48.43, False),
        ("负数不该命中正数", "同比增减 -48.43", 48.43, False),
        ("18.02 应命中 18.02", "毛利率 18.02", 18.02, True),
        ("-48.43 应命中 -48.43", "同比增减 -48.43", -48.43, True),
        ("千分位写法应命中", "营业收入 4,202,030,347.42", 4202030347.42, True),
        ("小数位不对不算命中", "每10股派息 0.15", 0.155, False),
        ("12.7 不该命中 12.74", "毛利率 12.74%", 12.7, False),
        ("18.02 不该命中 18.025", "毛利率 18.025", 18.02, False),
        ("末尾补零算同一个数", "每10 股派发现金红利0.10 元", 0.1, True),
        ("整数写成 5.00 算同一个数", "每股 5.00 元", 5.0, True),
        ("被拆行的数字要能认出来", "营业收入 1243188,17\n0.21 元", 1243188170.21, True),
        ("拆行也不许把两格粘成新数", "1.77 6.62", 1.776, False),
    ]
    for name, page_text, value, want in cases:
        got = rde._value_on_page(page_text, value)
        _ok(f"{name}（{'应命中' if want else '不该命中'}）", got == want, f"实得 {got}")


def section6() -> None:
    print("⑥ 反例：缓存按输入失效（P1-11）与多报的错误裁决进验收（P1-10）")
    import atexit

    from fintrace_engine import site as S

    tmp = ROOT / "data/annotations/cases/_guardrail_cache_tmp.json"
    atexit.register(lambda: tmp.unlink(missing_ok=True))
    case = json.loads(TXX_CASE.read_text(encoding="utf-8"))
    case["case_id"] = "GUARDRAIL-CACHE-TMP"
    case["split_mode"] = "rules"
    cid = case["case_id"]

    def write_case(cutoff: str) -> None:
        case["prediction_cutoff_date"] = cutoff
        tmp.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")

    def excluded(d: dict) -> int:
        return len(d["stages"]["stage2_materials"]["excluded"])

    # --- 结果缓存：改了案件字段必须重算（旧行为只看样本号）
    S.forget_case(cid)
    write_case("2024-04-22")
    a = S.run_case(cid)
    write_case("2020-01-01")          # 所有材料都应晚于这个截止日被排除
    b = S.run_case(cid)
    legacy_hit = cid in {cid: 1}      # 旧命中判断：case_id in _RUN_CACHE
    _ok("改案件字段后结果缓存失效（旧：只看样本号仍返回旧裁决 → 新：重算）",
        a["input_fingerprint"] != b["input_fingerprint"] and b is not a
        and excluded(b) > excluded(a) and legacy_hit,
        f"指纹 {a['input_fingerprint']} → {b['input_fingerprint']}，"
        f"截止日排除 {excluded(a)} → {excluded(b)} 份")

    # --- 输入指纹：必须覆盖案件字段（旧实现只哈希 案例 id + 草稿 + 材料）
    write_case("2024-04-22")
    fpa = S.case_input_fingerprint(cid)
    write_case("2020-01-01")
    fpb = S.case_input_fingerprint(cid)
    _ok("输入指纹覆盖案件字段（旧：改截止日指纹不变 → 新：变）", fpa != fpb,
        f"{fpa} → {fpb}")

    # --- 重新拆解改了主张集：产物指纹必须跟着变（原始输入一个字节都没变）
    import types

    real_split, real_run = S.split_draft, S.draft_review.run_draft_case
    try:
        def _claims(n: int) -> list:
            return [{"claim_id": f"DRAFT-C{i:03d}", "metric": "营业收入",
                     "metric_key": "revenue", "claim_value": float(i), "claim_unit": "亿元",
                     "verdict": {"label": "SUPPORTED", "error_type": None, "evidence": []}}
                    for i in range(1, n + 1)]

        got = {}
        for n in (14, 13):
            S.split_draft = (lambda nn: lambda case, *, resplit=False: (
                _claims(nn), {"mode": "rules", "how": "rules", "model": "", "from_": "fresh",
                              "degrade": "", "upgrade": ""}))(n)
            S.draft_review.run_draft_case = (lambda nn: lambda c, **kw: types.SimpleNamespace(
                to_dict=lambda: {"claims": _claims(nn)}))(n)
            S._RUN_CACHE.pop(cid, None)
            got[n] = S.run_case(cid, resplit=True)["artifact_fingerprint"]
        _ok("重新拆解改了主张集（14 → 13 条）也要换产物指纹（旧：只比原始输入 → 指纹不变）",
            got[14] != got[13], f"{got[14]} → {got[13]}")
    finally:
        S.split_draft, S.draft_review.run_draft_case = real_split, real_run
        S._RUN_CACHE.pop(cid, None)

    # --- 材料指纹：同一 tick 内改写、大小不变，也必须认出来
    pf = ROOT / "out" / "_guardrail_fp_probe.bin"
    pf.parent.mkdir(parents=True, exist_ok=True)
    atexit.register(lambda: pf.unlink(missing_ok=True))
    pf.write_bytes(b"AAAA")
    h1 = dr._file_fingerprint(pf)
    pf.write_bytes(b"BBBB")          # 同尺寸、同一时间 tick（旧行为：大小+秒级 mtime 认不出）
    h2 = dr._file_fingerprint(pf)
    st = pf.stat()
    _ok("同 tick 内改写同尺寸文件要能认出（旧：只比大小与修改时间 → 认不出；新：内容哈希）",
        h1 != h2, f"{h1} → {h2}（size {st.st_size}，mtime_ns {st.st_mtime_ns}）")

    # --- 截图缓存：键里必须带输入指纹（旧键只有 样本号/条号/序号/full）
    S.forget_case(cid)
    write_case("2024-04-22")
    data = S.run_case(cid)
    claim = next((c for c in data["claims"] if (c["verdict"].get("evidence") or [])), None)
    if claim is None:
        _ok("截图缓存键含输入指纹", False, "样本里没有带证据的主张")
    else:
        S.shot_png(cid, claim["claim_id"], 0, full=True)
        keys = [k for k in S._SHOT_CACHE if k[0] == cid]
        _ok("截图缓存键含产物指纹（旧键：样本号/条号/序号/full）",
            bool(keys) and keys[0][1] == data["artifact_fingerprint"] and len(keys[0]) == 5,
            f"键 = {keys[0] if keys else None}")
    S.forget_case(cid)
    tmp.unlink(missing_ok=True)

    # --- 评测口径：多报的错误裁决必须进验收
    case_d = json.loads(TXX_CASE.read_text(encoding="utf-8"))
    res = dr.run_draft_case(case_d, use_llm=False)
    fake = json.loads(json.dumps(res.claims[0], default=str))
    fake.update({"claim_id": "INJECTED-C999", "claim_value": 999999.0, "claim_unit": "元",
                 "period": "2023A",
                 "verdict": {"label": "CONTRADICTED", "error_type": "numeric_mismatch",
                             "evidence": [], "basis": "反例注入的未匹配矛盾裁决"}})
    res.claims.append(fake)          # 只在内存副本里注入，不落盘
    exp_path = ROOT / case_d["expected"]

    partial = dr.evaluate(res, exp_path)
    ex_p = partial["extra_outputs"]
    _ok("金标未声明完整（partial）：多报裁决列待审、不计 FP",
        ex_p["as_error"] >= 1 and ex_p["needs_review"] >= 1 and ex_p["as_fp"] == 0,
        f"as_error={ex_p['as_error']} 待审={ex_p['needs_review']} 计入FP={ex_p['as_fp']}")

    exp_full = json.loads(exp_path.read_text(encoding="utf-8"))
    exp_full["annotation_scope"] = "exhaustive"
    tmp_exp = ROOT / "out" / "_guardrail_expected_complete.json"
    tmp_exp.write_text(json.dumps(exp_full, ensure_ascii=False, indent=2), encoding="utf-8")
    atexit.register(lambda: tmp_exp.unlink(missing_ok=True))
    full = dr.evaluate(res, tmp_exp)
    ex_f = full["extra_outputs"]
    _ok("金标声明完整（exhaustive）：同一条计入 FP",
        ex_f["as_fp"] >= 1 and full["error_detection"]["fp"] >= 1,
        f"as_error={ex_f['as_error']} 计入FP={ex_f['as_fp']} "
        f"error_detection.fp={full['error_detection']['fp']}")

    import run_draft_eval as rde
    zero = {k: 0 for k in ("type_bad", "fix_bad", "fix_absent", "page_bad", "page_absent",
                           "pdf_bad", "pdf_unverified")}
    code_p, _why_p = rde.verdict_of(dict(zero), {"fp": 0, "review": 1, "incomplete": ["S"]})
    code_f, _why_f = rde.verdict_of(dict(zero), {"fp": 1, "review": 0, "incomplete": []})
    code_clean, why_clean = rde.verdict_of(dict(zero),
                                           {"fp": 0, "review": 0, "incomplete": []})
    _ok("有未处置的多报裁决 → 退出码 1（旧：只算内容断言，返回 0 还打印「两层都过了」）",
        code_p == 1 and code_f == 1 and code_clean == 0 and "两层都过了" in why_clean,
        f"待审 → 退出码 {code_p}；计入 FP → 退出码 {code_f}；干净 → 退出码 {code_clean}")


def main() -> int:
    for fn in (section1, section2, section3, section4, section5, section6):
        fn()
        print()
    if FAILS:
        print(f"！！ 有 {len(FAILS)} 条反例没过：" + "；".join(FAILS))
        return 1
    print("全部反例符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
