"""模块 C 必测 —— facts/store.py（公司事实库，SQLite）。

覆盖 BUILD_SPEC §3 的不变量：
- 幂等（同 id 重复写不重复、不报错）；
- only_facts 只返回双轴都过的条目，候选/未绑定仍落库留痕；
- (subject, period, metric) 三元组索引命中；
- cross_case_rule_usage 跨两个 run 统计；
- 进程重启（新建同一 db_path 实例）后数据仍在。
"""
from __future__ import annotations

from facts_factories import make_assertion, make_relation

from fintrace_agent.facts import FactStore


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
def test_put_assertion_idempotent(store):
    """同一 assertion_id 写两次：只有一行、不报错、内容以最新为准。"""
    a1 = make_assertion("A1")
    assert store.put_assertion(a1) == "A1"

    a1_again = make_assertion("A1")
    assert store.put_assertion(a1_again) == "A1"  # 不抛异常

    assert store.stats()["assertions"] == 1
    assert len(store.query()) == 1

    # 同 id 生命周期推进：candidate -> attested（内容以最新为准）
    a1_before = make_assertion("A1", evidence_status="candidate", semantic_status="unbound")
    store.put_assertion(a1_before)
    assert store.stats()["assertions"] == 1
    assert store.get_assertion("A1").evidence_status == "candidate"
    assert store.query() == []  # 双轴没过 -> 查不出事实


def test_put_relation_idempotent(store):
    r1 = make_relation("R1")
    assert store.put_relation(r1) == "R1"
    assert store.put_relation(make_relation("R1")) == "R1"
    assert store.stats()["relations"] == 1


# ---------------------------------------------------------------------------
# 点查 / 往返
# ---------------------------------------------------------------------------
def test_get_assertion_roundtrip(store):
    a = make_assertion("A1")
    store.put_assertion(a)
    got = store.get_assertion("A1")
    assert got is not None
    assert got.to_dict() == a.to_dict()          # 字段级往返一致
    assert got.is_fact is True
    assert got.can_support_contradiction is True
    assert store.get_assertion("missing") is None


def test_get_relation_roundtrip(store):
    r = make_relation("R1")
    store.put_relation(r)
    got = store.get_relation("R1")
    assert got is not None
    assert got.to_dict() == r.to_dict()
    assert got.applicable is True
    assert got.failed_fields == []
    assert store.get_relation("missing") is None


def test_relation_applicability_preserved(store):
    """适用条件（方案维度）必须原样存取：failed/unknown 不得被抹平。"""
    from fintrace_agent.contracts import ApplicabilityField, MatchResult

    r = make_relation(
        "R2",
        applicability={
            "caliber": ApplicabilityField(claim="母公司", evidence="合并", match=MatchResult.FAILED.value),
            "period": ApplicabilityField(claim="2023A", evidence="2022A", match=MatchResult.UNKNOWN.value),
        },
    )
    store.put_relation(r)
    got = store.get_relation("R2")
    assert got.applicable is False
    assert got.failed_fields == ["caliber"]
    assert got.field_ok("period") is False


# ---------------------------------------------------------------------------
# only_facts 过滤 + 留痕
# ---------------------------------------------------------------------------
def test_only_facts_filters_candidate_and_unbound(store):
    fact = make_assertion("FACT")                                   # attested + bound
    cand = make_assertion("CAND", evidence_status="candidate", semantic_status="bound")
    unbound = make_assertion("UNB", evidence_status="attested", semantic_status="unbound")
    conflicted = make_assertion("CONF", evidence_status="attested", semantic_status="conflicted")
    rejected = make_assertion("REJ", evidence_status="rejected", semantic_status="unbound")
    for a in (fact, cand, unbound, conflicted, rejected):
        store.put_assertion(a)

    ids = {a.assertion_id for a in store.query(only_facts=True)}
    assert ids == {"FACT"}                       # 只有双轴都过的才当事实

    all_ids = {a.assertion_id for a in store.query(only_facts=False)}
    assert all_ids == {"FACT", "CAND", "UNB", "CONF", "REJ"}   # 全部留痕可见

    # 留痕也体现在统计里
    st = store.stats()
    assert st["assertions"] == 5
    assert st["facts"] == 1
    assert st["by_evidence_status"] == {"attested": 3, "candidate": 1, "rejected": 1}


# ---------------------------------------------------------------------------
# 三元组索引命中
# ---------------------------------------------------------------------------
def test_query_triple_index_hit(store):
    store.put_assertion(make_assertion("A1", subject="启明信息", period="2023A", metric="现金分红金额"))
    store.put_assertion(make_assertion("A2", subject="启明信息", period="2023A", metric="净利润"))
    store.put_assertion(make_assertion("A3", subject="启明信息", period="2022A", metric="现金分红金额"))
    store.put_assertion(make_assertion("A4", subject="天下秀", period="2023A", metric="现金分红金额"))

    # 完整三元组唯一命中
    hit = store.query(subject="启明信息", period="2023A", metric="现金分红金额")
    assert [a.assertion_id for a in hit] == ["A1"]

    # 只给部分键
    assert {a.assertion_id for a in store.query(subject="启明信息")} == {"A1", "A2", "A3"}
    assert {a.assertion_id for a in store.query(subject="启明信息", period="2023A")} == {"A1", "A2"}
    assert {a.assertion_id for a in store.query(metric="现金分红金额")} == {"A1", "A3", "A4"}

    # 空过滤 = 全量
    assert len(store.query()) == 4
    # 未命中返回空
    assert store.query(subject="不存在") == []


def test_query_kind_filter(store):
    store.put_assertion(make_assertion("A1", kind="financial_value"))
    store.put_assertion(make_assertion("A2", kind="operating_event"))
    hits = store.query(kind="operating_event")
    assert [a.assertion_id for a in hits] == ["A2"]
    # 组合：kind 与其他键一起生效
    assert store.query(subject="启明信息", kind="financial_value")[0].assertion_id == "A1"


# ---------------------------------------------------------------------------
# relations_for
# ---------------------------------------------------------------------------
def test_relations_for(store):
    store.put_assertion(make_assertion("A1"))
    store.put_assertion(make_assertion("A2"))
    # R1: A1 -> A2；R2: A2 -> A3；R3: A3 -> A4（与 A1 无关）
    store.put_relation(make_relation("R1", subject_ref="A1", object_ref="A2"))
    store.put_relation(make_relation("R2", subject_ref="A2", object_ref="A3"))
    store.put_relation(make_relation("R3", subject_ref="A3", object_ref="A4"))

    assert [r.relation_id for r in store.relations_for("A1")] == ["R1"]
    assert {r.relation_id for r in store.relations_for("A2")} == {"R1", "R2"}  # 两端都算
    assert store.relations_for("不存在") == []


# ---------------------------------------------------------------------------
# 跨案件统计
# ---------------------------------------------------------------------------
def test_cross_case_rule_usage_two_runs(store):
    rule = "frm.cash_dividend_per_share"
    # 同一 rule_id 出现在两个不同 run 上
    store.put_relation(make_relation("R1", rule_id=rule, run_id="FT-20261010-aaaaaaaa"))
    store.put_relation(make_relation("R2", rule_id=rule, run_id="FT-20261011-bbbbbbbb"))
    # 另一条规则混入，不应被计入
    store.put_relation(make_relation("R3", rule_id="frm.net_profit_margin", run_id="FT-20261011-bbbbbbbb"))

    used = store.cross_case_rule_usage(rule)
    assert used == ["FT-20261010-aaaaaaaa", "FT-20261011-bbbbbbbb"]     # 跨两个 run
    assert store.cross_case_rule_usage("frm.net_profit_margin") == ["FT-20261011-bbbbbbbb"]
    assert store.cross_case_rule_usage("不存在") == []

    # 同一 run 重复用同一条规则只算一次（去重）
    store.put_relation(make_relation("R4", rule_id=rule, run_id="FT-20261010-aaaaaaaa"))
    assert store.cross_case_rule_usage(rule) == ["FT-20261010-aaaaaaaa", "FT-20261011-bbbbbbbb"]


# ---------------------------------------------------------------------------
# 进程重启 / 持久化
# ---------------------------------------------------------------------------
def test_persistence_across_restart(db_path):
    s1 = FactStore(db_path)
    s1.put_assertion(make_assertion("A1"))
    s1.put_assertion(make_assertion("CAND", evidence_status="candidate", semantic_status="unbound"))
    s1.put_relation(make_relation("R1", rule_id="rule.x", run_id="FT-1"))
    s1.close()

    # 新建同一 db_path 的实例 = 模拟进程重启
    s2 = FactStore(db_path)
    assert s2.get_assertion("A1").subject == "启明信息"
    assert [a.assertion_id for a in s2.query()] == ["A1"]           # 事实仍可查
    assert {a.assertion_id for a in s2.query(only_facts=False)} == {"A1", "CAND"}
    assert s2.cross_case_rule_usage("rule.x") == ["FT-1"]
    assert s2.stats()["assertions"] == 2
    s2.close()


# ---------------------------------------------------------------------------
# stats / 生命周期
# ---------------------------------------------------------------------------
def test_stats_shape(store):
    assert store.stats() == {
        "assertions": 0,
        "facts": 0,
        "relations": 0,
        "by_evidence_status": {},
        "by_semantic_status": {},
        "runs": [],
        "subjects": [],
    }
    store.put_assertion(make_assertion("A1", run_id="FT-1"))
    store.put_relation(make_relation("R1", run_id="FT-2"))
    st = store.stats()
    assert st["assertions"] == 1 and st["relations"] == 1 and st["facts"] == 1
    assert st["runs"] == ["FT-1", "FT-2"]        # 断言与关系里的 run 都计入
    assert st["subjects"] == ["启明信息"]


def test_context_manager_closes(db_path):
    with FactStore(db_path) as s:
        s.put_assertion(make_assertion("A1"))
    # 关闭后重开仍能读到（落盘成功）
    s2 = FactStore(db_path)
    assert s2.get_assertion("A1") is not None
    s2.close()
