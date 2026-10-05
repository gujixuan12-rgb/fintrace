"""标注自检：核对 data/annotations/evaluation/*.json 的每条证据是否真的能在源 PDF 上找到。

标注自身出错，评测就失效。此脚本做三件事：
  1. 核对每条证据的 pdf_page 上是否真包含所写片段（去空白后子串匹配）；
  2. 对「引用页码错」类断言做反证检查（该页确实不含被引内容）；
  3. 检查标注不变量：证据不足类不得附正面证据，矛盾类必须给修改建议。

用法（仓库根目录）：python tools/audit_annotation.py
"""
import json
import re
import sys
from pathlib import Path

import fitz  # pymupdf

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "data/annotations/evaluation/qiming_2023_research_expected.json"
PDF = ROOT / "data/raw/002232/01_改错前_2023年年度报告.pdf"

# 每条证据要在指定页上找到的片段（去空白后做子串匹配）
CHECKS = {
    "QMD-001": [(7, ["1,243,188,170.21", "-19.33%"]), (14, ["124,318.82 万元", "减少19.33%"])],
    "QMD-002": [(7, ["29,315,603.26", "-59.93%"]), (15, ["2931.56 万元", "减少59.93%"])],
    "QMD-003": [(237, ["0.0718"]), (7, ["29,315,603.26"])],
    "QMD-004": [(51, ["408,548.46", "408548455", "0.10"]), (223, ["408.55 万元"]), (2, ["408,548,455", "0.10 元"])],
    "QMD-005": [(8, ["1,385,610,539.28", "0.23%"])],
    "QMD-006": [(237, ["0.0718"]), (88, ["净利润"])],
    "QMD-007": [(237, ["2.12%", "1.66%", "0.0564"])],
    "QMD-008": [(16, ["1,239,720,956.43", "1,064,066,560.88", "14.17%", "3.68%"])],
    "QMD-009": [(15, ["28,668.13 万元", "474.79%"])],
    "QMD-010": [(8, ["2,177,194,049.11", "-2.17%"])],
    "QMD-011": [(15, ["2,232.63 万元", "82.19%"])],
    "QMD-012": [(15, ["5,580.05 万元", "35.26%"])],
    "QMD-013": [],  # 证据不足类：材料集内不存在股价来源，无正面证据
}

# 引用页码错类断言的反证：这些页不应出现被引内容
NEGATIVE = {
    "QMD-006": [(88, "每股收益")],
}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def main() -> int:
    data = json.loads(EXPECTED.read_text(encoding="utf-8"))
    ids = [c["claim_id"] for c in data["claims"]]

    if not PDF.exists():
        print(f"!! 源 PDF 不存在：{PDF}")
        return 2

    missing = set(CHECKS) - set(ids)
    extra = set(ids) - set(CHECKS)
    if missing or extra:
        print(f"!! 自检表与标注不同步  缺:{missing} 多:{extra}")

    doc = fitz.open(PDF)
    pages = {i + 1: norm(doc[i].get_text()) for i in range(doc.page_count)}
    ok = bad = 0
    for cid in ids:
        checks = CHECKS.get(cid)
        if checks is None:
            continue
        for page, frags in checks:
            text = pages.get(page, "")
            for frag in frags:
                if norm(frag) in text:
                    ok += 1
                else:
                    bad += 1
                    print(f"[不通过] {cid}  p{page}  找不到「{frag}」")
        n = sum(len(f) for _, f in checks)
        print(f"  {cid}  已核对 {n} 项")

    print("\n-- 反证检查（引用页码错类）--")
    for cid, items in NEGATIVE.items():
        for page, frag in items:
            if norm(frag) not in pages.get(page, ""):
                print(f"  通过: {cid}  p{page} 确实不含「{frag}」")
            else:
                bad += 1
                print(f"[不通过] {cid}  p{page} 意外含「{frag}」")
    doc.close()

    # 不变量
    inv_bad = 0
    for c in data["claims"]:
        if c["expected_verdict"] == "INSUFFICIENT" and c.get("evidence"):
            inv_bad += 1
            print(f"[不变量违反] {c['claim_id']} 判证据不足却附了正面证据")
        if c["expected_verdict"] == "CONTRADICTED" and not c.get("suggested_fix"):
            inv_bad += 1
            print(f"[不变量违反] {c['claim_id']} 判矛盾却无修改建议")

    print(f"\n汇总: 证据核对通过 {ok} 项，不通过 {bad} 项；不变量违反 {inv_bad} 项")
    return 1 if (bad or inv_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
