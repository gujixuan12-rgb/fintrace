"""公司事实库（实例层）—— 模块 C，对应 BUILD_SPEC 第 3 节。

设计要点（不变量，见 BUILD_SPEC §3）：

1. **双轴状态**：每条 ``Assertion`` 带 ``evidence_status``（W 门，轴一）与
   ``semantic_status``（B 系列语义绑定，轴二）。``only_facts=True`` 时只返回
   两轴都过的条目：``evidence_status == "attested"`` 且 ``semantic_status == "bound"``，
   等价于 ``Assertion.is_fact``。
2. **留痕**：未过双轴的候选/未绑定条目**照常落库**（可被 ``only_facts=False`` 查到、
   被 ``stats`` 计数），只是不能当事实查出来用。
3. **幂等**：``put_assertion`` / ``put_relation`` 以主键（``assertion_id`` /
   ``relation_id``）为准。同一 id 重复写入不产生重复行、不报错；同一 id 再次写入会
   用最新内容覆盖（对应断言从 candidate→attested→bound 的生命周期推进），
   因为「内容相同再写一次」也仍是幂等的。
4. **索引**：以 ``(subject, period, metric)`` 建索引，``query`` 能按三元组（或其任意
   子集）命中。
5. **跨案件统计**：``cross_case_rule_usage`` 按 ``Relation.rule_id`` 汇总，
   返回涉及的不同 ``run_id``（可跨多个 run），是为将来换 PG 留的触发口。
6. **持久化**：数据落在调用方指定的 SQLite 文件里，进程重启（新建同一 ``db_path``
   的实例）后数据仍在。

只用标准库（``sqlite3``）；核心链路零第三方依赖是项目不变量。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from ..contracts import Assertion, Relation

#: 双轴的「事实」取值：与 contracts.EvidenceStatus.ATTESTED / SemanticStatus.BOUND 一致。
_EVIDENCE_FACT = "attested"
_SEMANTIC_FACT = "bound"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assertions (
    assertion_id    TEXT PRIMARY KEY,
    subject         TEXT NOT NULL DEFAULT '',
    period          TEXT NOT NULL DEFAULT '',
    metric          TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT '',
    evidence_status TEXT NOT NULL DEFAULT '',
    semantic_status TEXT NOT NULL DEFAULT '',
    run_id          TEXT NOT NULL DEFAULT '',
    payload         TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_assertions_spm
    ON assertions (subject, period, metric);
CREATE INDEX IF NOT EXISTS idx_assertions_run
    ON assertions (run_id);

CREATE TABLE IF NOT EXISTS relations (
    relation_id   TEXT PRIMARY KEY,
    predicate     TEXT NOT NULL DEFAULT '',
    subject_ref   TEXT NOT NULL DEFAULT '',
    object_ref    TEXT NOT NULL DEFAULT '',
    rule_id       TEXT NOT NULL DEFAULT '',
    rule_version  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    run_id        TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_relations_subject
    ON relations (subject_ref);
CREATE INDEX IF NOT EXISTS idx_relations_object
    ON relations (object_ref);
CREATE INDEX IF NOT EXISTS idx_relations_rule
    ON relations (rule_id);
"""


def _now() -> str:
    """本地时间戳（秒级即可）；仅用于留痕，不参与任何判定。"""
    import datetime as _dt

    return _dt.datetime.now().isoformat(timespec="seconds")


class FactStore:
    """SQLite 支撑的公司事实库。

    典型用法::

        store = FactStore(Path("facts.sqlite"))
        store.put_assertion(a)
        facts = store.query(subject="启明信息", period="2023A", metric="现金分红金额")
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        # 允许内存库（":memory:"）与文件库；文件库先建父目录。
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def put_assertion(self, a: Assertion) -> str:
        """写入一条断言，返回 ``assertion_id``（按 id 幂等）。

        主键冲突时用新内容覆盖（生命周期推进），不产生重复行、不报错。
        """
        payload = json.dumps(a.to_dict(), ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO assertions
                (assertion_id, subject, period, metric, kind,
                 evidence_status, semantic_status, run_id, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(assertion_id) DO UPDATE SET
                subject         = excluded.subject,
                period          = excluded.period,
                metric          = excluded.metric,
                kind            = excluded.kind,
                evidence_status = excluded.evidence_status,
                semantic_status = excluded.semantic_status,
                run_id          = excluded.run_id,
                payload         = excluded.payload,
                updated_at      = excluded.updated_at
            """,
            (
                a.assertion_id, a.subject, a.period, a.metric, a.kind,
                a.evidence_status, a.semantic_status, a.run_id, payload, _now(),
            ),
        )
        self._conn.commit()
        return a.assertion_id

    def put_relation(self, r: Relation) -> str:
        """写入一条关系，返回 ``relation_id``（按 id 幂等）。"""
        payload = json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO relations
                (relation_id, predicate, subject_ref, object_ref, rule_id,
                 rule_version, status, run_id, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relation_id) DO UPDATE SET
                predicate    = excluded.predicate,
                subject_ref  = excluded.subject_ref,
                object_ref   = excluded.object_ref,
                rule_id      = excluded.rule_id,
                rule_version = excluded.rule_version,
                status       = excluded.status,
                run_id       = excluded.run_id,
                payload      = excluded.payload,
                updated_at   = excluded.updated_at
            """,
            (
                r.relation_id, r.predicate, r.subject_ref, r.object_ref,
                r.rule_id, r.rule_version, r.status, r.run_id, payload, _now(),
            ),
        )
        self._conn.commit()
        return r.relation_id

    # ------------------------------------------------------------------
    # 点查
    # ------------------------------------------------------------------
    def get_assertion(self, assertion_id: str) -> Optional[Assertion]:
        row = self._conn.execute(
            "SELECT payload FROM assertions WHERE assertion_id = ?",
            (assertion_id,),
        ).fetchone()
        if row is None:
            return None
        return Assertion.from_dict(json.loads(row["payload"]))

    def get_relation(self, relation_id: str) -> Optional[Relation]:
        row = self._conn.execute(
            "SELECT payload FROM relations WHERE relation_id = ?",
            (relation_id,),
        ).fetchone()
        if row is None:
            return None
        return Relation.from_dict(json.loads(row["payload"]))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        *,
        subject: str = "",
        period: str = "",
        metric: str = "",
        only_facts: bool = True,
        kind: str = "",
    ) -> list[Assertion]:
        """按 ``(subject, period, metric)`` 三元组（可给子集）查询断言。

        空串表示「该字段不筛选」。``only_facts=True`` 时只返回双轴都过的条目；
        ``only_facts=False`` 时把候选/未绑定条目也一并返回（留痕可见）。
        """
        where: list[str] = []
        params: list = []
        if subject:
            where.append("subject = ?")
            params.append(subject)
        if period:
            where.append("period = ?")
            params.append(period)
        if metric:
            where.append("metric = ?")
            params.append(metric)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if only_facts:
            where.append("evidence_status = ?")
            params.append(_EVIDENCE_FACT)
            where.append("semantic_status = ?")
            params.append(_SEMANTIC_FACT)

        sql = "SELECT payload FROM assertions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY subject, period, metric, assertion_id"

        rows = self._conn.execute(sql, params).fetchall()
        return [Assertion.from_dict(json.loads(row["payload"])) for row in rows]

    def relations_for(self, assertion_id: str) -> list[Relation]:
        """返回与该断言相关的关系（端点命中 ``subject_ref`` 或 ``object_ref``）。"""
        rows = self._conn.execute(
            """
            SELECT payload FROM relations
            WHERE subject_ref = ? OR object_ref = ?
            ORDER BY relation_id
            """,
            (assertion_id, assertion_id),
        ).fetchall()
        return [Relation.from_dict(json.loads(row["payload"])) for row in rows]

    # ------------------------------------------------------------------
    # 跨案件统计
    # ------------------------------------------------------------------
    def cross_case_rule_usage(self, rule_id: str) -> list[str]:
        """某条规则（``rule_id``）在哪些 run 上被用过 —— 跨案件统计。

        返回去重、升序的 ``run_id`` 列表（空 run_id 不计入）。
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT run_id FROM relations
            WHERE rule_id = ? AND run_id <> ''
            ORDER BY run_id
            """,
            (rule_id,),
        ).fetchall()
        return [row["run_id"] for row in rows]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """概览：总数、事实数、双轴分布、涉及 run 与 subject。"""
        total_a = self._conn.execute("SELECT COUNT(*) AS n FROM assertions").fetchone()["n"]
        total_r = self._conn.execute("SELECT COUNT(*) AS n FROM relations").fetchone()["n"]
        facts = self._conn.execute(
            "SELECT COUNT(*) AS n FROM assertions WHERE evidence_status = ? AND semantic_status = ?",
            (_EVIDENCE_FACT, _SEMANTIC_FACT),
        ).fetchone()["n"]

        by_ev = {
            row["evidence_status"]: row["n"]
            for row in self._conn.execute(
                "SELECT evidence_status, COUNT(*) AS n FROM assertions "
                "GROUP BY evidence_status ORDER BY evidence_status"
            )
        }
        by_sem = {
            row["semantic_status"]: row["n"]
            for row in self._conn.execute(
                "SELECT semantic_status, COUNT(*) AS n FROM assertions "
                "GROUP BY semantic_status ORDER BY semantic_status"
            )
        }
        runs = [
            row["run_id"]
            for row in self._conn.execute(
                "SELECT DISTINCT run_id FROM assertions WHERE run_id <> '' "
                "UNION SELECT DISTINCT run_id FROM relations WHERE run_id <> '' "
                "ORDER BY 1"
            )
        ]
        subjects = [
            row["subject"]
            for row in self._conn.execute(
                "SELECT DISTINCT subject FROM assertions WHERE subject <> '' ORDER BY subject"
            )
        ]
        return {
            "assertions": total_a,
            "facts": facts,
            "relations": total_r,
            "by_evidence_status": by_ev,
            "by_semantic_status": by_sem,
            "runs": runs,
            "subjects": subjects,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FactStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["FactStore"]
