"""巨潮资讯网取数客户端（标准库实现，无第三方依赖）。

能力：

    client = CninfoClient(cache_dir="data/sourced", rate=1.0)
    client.stock_map()                       # {code: {"orgId":..., "name":...}}（带磁盘缓存）
    client.announcements(code="002232", category="ndbg", limit=20)
    client.entries(code="002232", keyword="更正", se_date="2024-01-01~2025-12-31")
    client.fetch(code="002232", category="ndbg", dry_run=True)   # 下载 + 登记 manifest

人为可控的开关（也是 CLI 的参数）：`limit`（条数上限）、`rate`（请求间隔秒）、
`se_date`（披露日期区间）、`keyword`（标题关键词）、`dry_run`（只列不下载）、
`overwrite`（默认不覆盖已下载文件，天然断点续传）。

接口形状实测见技能 `a-share-disclosure-sourcing`。两个已知事实：

* 全市场映射表 `new/data/szse_stock.json` 用 urllib 可正常取回（约 592 KB / 6259 条），
  但 curl 在同一 URL 上会写出 0 字节文件 —— 是 curl 的写盘问题，不是接口问题。
* `stock` 参数必须是 `<code>,<orgId>` 两段式；orgId 不能按代码前缀猜
  （002232 实际是 `9900004567`），一律从映射表取。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

#: 巨潮的 announcementTime 是东八区墙上时间的毫秒戳，转日期必须按 +08:00，不能用 UTC。
_CST = timezone(timedelta(hours=8))

BASE = "http://www.cninfo.com.cn"
STATIC = "http://static.cninfo.com.cn"
STOCK_MAP_URL = BASE + "/new/data/szse_stock.json"
QUERY_URL = BASE + "/new/hisAnnouncement/query"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# 类别简称 → 巨潮 category 参数。更正公告没有独立 category，用 keyword="更正" 取。
CATEGORIES: dict[str, str] = {
    # 季度报告在巨潮分「一季度报告 / 三季度报告」两类，都要能查
    "ndbg": "category_ndbg_szsh",      # 年度报告
    "bndbg": "category_bndbg_szsh",    # 半年度报告
    "yjdbg": "category_yjdbg_szsh",    # 一季度报告
    "sjdbg": "category_sjdbg_szsh",    # 三季度报告
    "sjbg": "category_sjbg_szsh",      # 审计报告
    "": "",                            # 留空 = 不按类别过滤（配 keyword 用）
}


class SourcingError(RuntimeError):
    """取数过程中的可预期失败（网络、参数、源端结构变化）。"""


def _strip_em(title: str) -> str:
    """去掉 searchkey 命中时源端加的高亮标签。"""
    return re.sub(r"</?em>", "", title or "")


def _safe_name(name: str, maxlen: int = 60) -> str:
    """把公告标题变成安全的文件名片段。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", _strip_em(name)).strip("_")
    return (cleaned or "untitled")[:maxlen]


@dataclass
class Announcement:
    """一条公告索引记录（尚未下载）。"""

    code: str
    name: str
    org_id: str
    announcement_id: str
    title: str
    published_at: str          # ISO 日期，来源是毫秒时间戳（东八区）
    url: str                   # PDF 直链
    size_kb: int
    category: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("raw", None)
        return d


def _ms_to_date(ms: Any) -> str:
    """毫秒时间戳 → 公告日期（东八区）。

    源端给的公告日期就是北京时间：按 UTC 取日期会把「北京时间上午 8 点前发布的公告」
    记成前一天，于是这条公告在截止日过滤里被提前一天放行（或误判成晚于截止日）。
    """
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, _CST).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class CninfoClient:
    """巨潮取数客户端。所有网络访问都经过 `_request`，速率与 UA 集中在这里。"""

    def __init__(
        self,
        cache_dir: str | Path = "data/sourced",
        rate: float = 1.0,
        timeout: float = 90.0,
        user_agent: str = DEFAULT_UA,
        verbose: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.rate = max(0.0, float(rate))
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.verbose = verbose
        self._last_request = 0.0
        self._stock_map: dict[str, dict[str, str]] | None = None
        self.requests_made = 0

    # ---------- 传输层 ----------

    def _throttle(self) -> None:
        if self.rate <= 0:
            return
        gap = time.monotonic() - self._last_request
        if gap < self.rate:
            time.sleep(self.rate - gap)
        self._last_request = time.monotonic()

    def _request(self, url: str, data: dict[str, str] | None = None) -> bytes:
        self._throttle()
        headers = {"User-Agent": self.user_agent, "Referer": BASE + "/"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"
        else:
            body = None
        req = urllib.request.Request(url, data=body, headers=headers)
        self.requests_made += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:  # pragma: no cover - 网络分支
            raise SourcingError(f"HTTP {e.code} 取 {url} 失败") from e
        except urllib.error.URLError as e:   # pragma: no cover - 网络分支
            raise SourcingError(f"取 {url} 失败：{e.reason}") from e

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ---------- 代码 → orgId ----------

    def stock_map(self, refresh: bool = False) -> dict[str, dict[str, str]]:
        """全市场 code → {orgId, name}。默认读本地缓存，refresh=True 时重新取。"""
        cache = self.cache_dir / "_stock_map.json"
        if self._stock_map is not None and not refresh:
            return self._stock_map
        if cache.exists() and not refresh:
            data = json.loads(cache.read_text(encoding="utf-8"))
            rows = data.get("stockList", data if isinstance(data, list) else [])
        else:
            raw = self._request(STOCK_MAP_URL)
            data = json.loads(raw.decode("utf-8"))
            rows = data.get("stockList", [])
            if not rows:
                raise SourcingError("映射表返回空，源端结构可能变化；不要据此断言接口不可用，先复测")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self._log(f"[sourcing] 刷新映射表：{len(rows)} 条 → {cache}")
        self._stock_map = {
            r["code"]: {"orgId": r.get("orgId", ""), "name": r.get("zwjc", "")}
            for r in rows if r.get("code")
        }
        return self._stock_map

    def resolve(self, code: str) -> dict[str, str]:
        """把股票代码解析成 {code, orgId, name}。解析不到就抛错，不猜。"""
        m = self.stock_map().get(code)
        if not m or not m.get("orgId"):
            raise SourcingError(f"映射表里找不到代码 {code}；不要按前缀猜 orgId")
        return {"code": code, "orgId": m["orgId"], "name": m["name"]}

    # ---------- 公告索引 ----------

    def announcements(
        self,
        code: str | None = None,
        category: str = "",
        keyword: str = "",
        se_date: str = "",
        column: str | None = None,
        limit: int = 30,
        page_size: int = 30,
        max_pages: int = 20,
    ) -> list[Announcement]:
        """查公告索引。

        `code` 给了就只查这家（自动补 orgId）；不给则按 category/keyword/se_date
        在 column 覆盖的范围内查（szse 实测覆盖深市与北交所）。
        """
        stock = ""
        if code:
            info = self.resolve(code)
            stock = f"{code},{info['orgId']}"
            column = column or _guess_column(code)
        else:
            column = column or "szse"
        cat = CATEGORIES.get(category, category)

        out: list[Announcement] = []
        page = 1
        while len(out) < limit and page <= max_pages:
            payload = {
                "stock": stock, "tabName": "fulltext", "pageSize": str(page_size),
                "pageNum": str(page), "column": column, "category": cat, "plate": "",
                "seDate": se_date, "searchkey": keyword, "secid": "",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            }
            body = json.loads(self._request(QUERY_URL, payload).decode("utf-8"))
            rows = body.get("announcements") or []
            if not rows:
                break
            for a in rows:
                out.append(Announcement(
                    code=a.get("secCode", ""), name=a.get("secName", ""),
                    org_id=a.get("orgId", ""), announcement_id=str(a.get("announcementId", "")),
                    title=_strip_em(a.get("announcementTitle", "")),
                    published_at=_ms_to_date(a.get("announcementTime")),
                    url=STATIC + "/" + (a.get("adjunctUrl") or ""),
                    size_kb=int(a.get("adjunctSize") or 0), category=cat, raw=a,
                ))
            if not body.get("hasMore") or len(out) >= limit:
                break
            page += 1
        return out[:limit]

    # ---------- 下载与登记 ----------

    def local_path(self, ann: Announcement) -> Path:
        return self.cache_dir / ann.code / f"{ann.published_at}_{ann.announcement_id}_{_safe_name(ann.title)}.PDF"

    def download(self, ann: Announcement, overwrite: bool = False) -> dict[str, Any]:
        """下载一份公告 PDF，返回 manifest 条目。默认不覆盖（= 断点续传）。"""
        dest = self.local_path(ann)
        if dest.exists() and not overwrite and dest.stat().st_size > 0:
            self._log(f"[sourcing] 已存在，跳过：{dest.name}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = self._request(ann.url)
            if len(blob) < 1024 or not blob[:4] == b"%PDF":
                raise SourcingError(f"下载结果不是 PDF（{len(blob)} 字节）：{ann.url}")
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(blob)
            tmp.replace(dest)
            self._log(f"[sourcing] 下载 {ann.published_at} {ann.title[:28]} → {dest.name} ({len(blob)} 字节)")
        blob = dest.read_bytes()
        entry = {**ann.as_dict(), "path": str(dest).replace("\\", "/"),
                 "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                 "pages": _pdf_pages(dest), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        return entry

    # ---------- manifest ----------

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    def fetch(
        self,
        code: str | None = None,
        category: str = "",
        keyword: str = "",
        se_date: str = "",
        limit: int = 20,
        min_size_kb: int = 0,
        dry_run: bool = False,
        overwrite: bool = False,
        title_must_include: Iterable[str] = (),
    ) -> dict[str, Any]:
        """一步到位：查索引 → （可选）过滤 → 下载 → 登记 manifest。

        `min_size_kb` 用来挡掉「摘要 / 补丁式」小公告，只留正文级材料。
        返回 {"candidates": [...], "downloaded": [...], "skipped": [...]}。
        dry_run=True 只查不下载，用于人工确认要抓什么。
        """
        anns = self.announcements(code=code, category=category, keyword=keyword,
                                  se_date=se_date, limit=limit)
        must = [s for s in title_must_include if s]
        if must:
            anns = [a for a in anns if all(s in a.title for s in must)]
        if min_size_kb:
            anns = [a for a in anns if a.size_kb >= min_size_kb]
        result: dict[str, Any] = {"candidates": [a.as_dict() for a in anns],
                                  "downloaded": [], "skipped": [], "errors": []}
        if dry_run:
            return result
        manifest = self.load_manifest()
        for a in anns:
            try:
                entry = self.download(a, overwrite=overwrite)
            except SourcingError as e:
                self._log(f"[sourcing] 失败：{a.title[:30]} —— {e}")
                result["errors"].append({"title": a.title, "error": str(e)})
                continue
            manifest[a.announcement_id] = entry
            result["downloaded"].append(entry)
        self.save_manifest(manifest)
        return result


def _guess_column(code: str) -> str:
    """6/9 开头走沪市，其余走深市（实测 szse 也覆盖北交所）。"""
    return "sse" if code[:1] in ("6", "9") else "szse"


def _pdf_pages(path: Path) -> int | None:
    """页数（可选依赖 pymupdf；没装就返回 None，不影响取数）。"""
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 取决于是否装 pdf extra
        return None
    try:
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:  # pragma: no cover - 损坏文件
        return None


def search_local(manifest: dict[str, Any], text: str = "", code: str = "",
                 category: str = "", date_from: str = "", date_to: str = "") -> list[dict[str, Any]]:
    """在 manifest 上做本地检索（不联网）。这是「方式可检索」的落点。"""
    hits = []
    for e in manifest.values():
        if code and e.get("code") != code:
            continue
        if category and e.get("category") != CATEGORIES.get(category, category):
            continue
        d = e.get("published_at", "")
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        if text and text not in (e.get("title", "") + e.get("name", "")):
            continue
        hits.append(e)
    hits.sort(key=lambda e: e.get("published_at", ""), reverse=True)
    return hits
