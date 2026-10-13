"""路径与目录约定（v2.2 §3.5）—— 全项目唯一拼路径的地方。

两类目录分开，别再各写一套：

* **项目资产**（案件、真值、知识库、事实库）：跟着项目根走，不随环境漂移。
* **运行时产物**（`runs/` `blobs/` `snapshots/`）：跟 `FINTRACE_HOME` 走，缺省等于项目根。
  测试用它把产物重定向到临时目录；部署时也能把产物挪到别的盘。
  **缺省是项目根而不是当前工作目录** —— 按 cwd 会让在别的目录启动的 CLI 把产物写到别处，
  事后按 run_id 找不回来。

历史遗留：`store/paths.py` 曾自己解析一遍（缺省 cwd），现已统一到本模块，并在那里保留同名
的 `home()/runs_dir()/blobs_dir()/snapshots_dir()` 供 store 包继续使用。
"""
from __future__ import annotations

import os
from pathlib import Path

#: 项目根：src/fintrace_agent/paths.py → parents[2]
ROOT: Path = Path(__file__).resolve().parents[2]

SRC: Path = ROOT / "src"
DOCS: Path = ROOT / "docs"
CASES_DIR: Path = ROOT / "data" / "annotations" / "cases"
EVAL_DIR: Path = ROOT / "data" / "annotations" / "evaluation"
DRAFTS_DIR: Path = ROOT / "data" / "annotations" / "drafts"
RAW_DIR: Path = ROOT / "data" / "raw"

KNOWLEDGE_DIR: Path = ROOT / "knowledge"
KNOWLEDGE_DB: Path = KNOWLEDGE_DIR / "knowledge.sqlite"
KNOWLEDGE_DATA: Path = KNOWLEDGE_DIR / "data"

FACTS_DB: Path = ROOT / "data" / "facts.sqlite"

SECRETS: Path = ROOT / ".secrets" / "llm.json"

#: 运行时产物文件（v2.2 §3.5）
RUN_FILES = ("state.json", "findings.json", "evidence.jsonl", "decisions.jsonl",
             "rejections.jsonl", "report.md")


# --- 运行时产物（调用时解析，便于测试重定向） --------------------------------


def home() -> Path:
    """运行时产物的根：`FINTRACE_HOME` 优先，缺省项目根。"""
    value = (os.environ.get("FINTRACE_HOME") or "").strip()
    return Path(value) if value else ROOT


def runs_dir() -> Path:
    """运行目录的父目录。"""
    return home() / "runs"


def blobs_dir() -> Path:
    """内容寻址对象库目录。"""
    return home() / "blobs"


def snapshots_dir() -> Path:
    """外部材料快照目录。"""
    return home() / "snapshots"


def run_dir(run_id: str, create: bool = False) -> Path:
    """`runs/<run_id>/`；create=True 时顺手建好 snapshots/ 与 tools/ 子目录。"""
    p = runs_dir() / str(run_id)
    if create:
        (p / "snapshots").mkdir(parents=True, exist_ok=True)
        (p / "tools").mkdir(parents=True, exist_ok=True)
    return p


def ensure_dirs() -> None:
    """建好运行时产物与知识库目录；幂等。项目资产目录随仓库提供，不在这里造。"""
    for d in (runs_dir(), blobs_dir(), snapshots_dir(), KNOWLEDGE_DIR, KNOWLEDGE_DATA,
              FACTS_DB.parent):
        d.mkdir(parents=True, exist_ok=True)


__all__ = [n for n in dir() if not n.startswith("_")]
