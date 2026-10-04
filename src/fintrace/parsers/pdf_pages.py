"""真实 PDF 取文：把每一段文字钉在「哪份文件、第几页」上。

这是最小链路（原始 PDF → 页码定位 → 字段结构化 → 确定性复算 → 可追溯发现）的地基。
页码一律用**物理页序号（1 起）**，同时保留原文行，便于人工复核。

依赖 pymupdf，属于可选依赖（`pip install -e ".[pdf]"`）；因此本模块只在真正读文件时
才导入它——没装 pymupdf 时，其余模块与测试照常可跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _pymupdf():
    """延迟导入 pymupdf，缺依赖时给出可执行的安装提示。"""
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ImportError(
            '读取真实 PDF 需要 pymupdf，请先安装：pip install -e ".[pdf]"'
        ) from exc
    return pymupdf


@dataclass(frozen=True)
class PageText:
    """一页文本 —— 带出处的最小单位。"""

    path: Path
    page: int          # 物理页，1 起
    raw: str           # 原始文本，保留换行（用于按行取标签/数值）
    bbox: tuple[float, float, float, float] | None = None

    @property
    def flat(self) -> str:
        """去掉全部空白，用于跨行匹配（年报里金额经常被断行拆开）。"""
        return "".join(self.raw.split())

    @property
    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.raw.splitlines() if ln.strip()]

    @property
    def source_file(self) -> str:
        return self.path.name

    def cite(self) -> str:
        """人类可读的出处，例如「2023年年度报告.pdf 第 51 页」。"""
        return f"{self.path.name} 第 {self.page} 页"


def load(path: str | Path) -> list[PageText]:
    """读入一份 PDF，返回逐页文本（带页码）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"材料不存在：{path}")
    pymupdf = _pymupdf()
    doc = pymupdf.open(str(path))
    try:
        pages: list[PageText] = []
        for i, page in enumerate(doc, 1):
            rect = page.rect
            pages.append(
                PageText(
                    path=path,
                    page=i,
                    raw=page.get_text(),
                    bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                )
            )
        return pages
    finally:
        doc.close()


def find_pages(pages: list[PageText], keyword: str) -> list[PageText]:
    """按关键词（忽略空白差异）定位页。"""
    key = "".join(keyword.split())
    return [p for p in pages if key in p.flat]


def first_page_of(pages: list[PageText], keyword: str) -> int | None:
    """关键词首次出现的物理页码；找不到返回 None。"""
    for p in pages:
        if "".join(keyword.split()) in p.flat:
            return p.page
    return None
