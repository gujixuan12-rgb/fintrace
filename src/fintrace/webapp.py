"""fintrace 最小 Web 核验台（标准库实现，不引入第三方依赖）。

用途：给人手动核验最小链路 —— 选一份年报 PDF，点一下，看结论、复算过程、
逐条发现（带页码与原文行）和整份报告。

安全边界：
- 只监听 127.0.0.1，不对外网开放；
- 只允许核查 data/raw 与 data/uploads 下的 PDF —— 这个服务不是通用文件读取器，
  不能让本机任意文件变成可下载资源。

启动：`python -m fintrace.cli ui --port 8911`
"""
from __future__ import annotations

import datetime
import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import dividend_case, llm

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ROOTS = (ROOT / "data" / "raw", ROOT / "data" / "uploads")
OUT_ROOT = ROOT / "out" / "ui"

DEFAULT_PORT = 8911

#: 启动时刻，页脚与 /api/health 用它区分「这是哪一个实例」——
#: Windows 上 SO_REUSEADDR 允许多个进程绑同一端口且不报错，
#: 重复启动时两个实例会抢连接，靠这个字段一眼看出跑的是哪份代码。
STARTED_AT = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fintrace 核查台</title>
<style>
:root{--ink:#1a2733;--pri:#1F4E79;--line:#d6e0ea;--bg:#f5f8fb;--err:#c0392b;--warn:#b9770e;--info:#4a6572;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 "Microsoft YaHei",Segoe UI,system-ui,sans-serif}
header{background:var(--pri);color:#fff;padding:15px 22px}
header h1{margin:0;font-size:17px;font-weight:600}
header p{margin:5px 0 0;font-size:12px;opacity:.85}
main{max-width:1060px;margin:18px auto;padding:0 16px 64px}
section{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin-bottom:14px}
h2{font-size:14px;margin:0 0 12px;color:var(--pri);border-bottom:1px solid var(--line);padding-bottom:8px}
label{display:block;font-size:12px;color:#5a6b7a;margin-bottom:4px}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:5px;font:inherit;background:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.wide{grid-column:1/-1}
button{background:var(--pri);color:#fff;border:0;border-radius:5px;padding:9px 22px;font:inherit;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
.banner{padding:11px 14px;border-radius:6px;font-weight:600;margin-bottom:12px}
.banner.ok{background:#e8f5ee;color:#1e7a4d}
.banner.bad{background:#fdecea;color:var(--err)}
.banner.fail{background:#fdf3e0;color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0 2px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:#eef3f8;color:#33506b;font-weight:600}
.finding{border-left:3px solid var(--line);padding:8px 0 8px 12px;margin:12px 0}
.finding.error{border-left-color:var(--err)}
.finding.warn{border-left-color:var(--warn)}
.finding.info{border-left-color:var(--info)}
.finding .code{font-family:Consolas,monospace;font-size:12px;color:#6b7c8b}
.error-t{color:var(--err);font-weight:600}
.warn-t{color:var(--warn);font-weight:600}
.info-t{color:var(--info)}
.ev{font-family:Consolas,monospace;font-size:12px;color:#4a5b6a;margin:3px 0 0 14px}
.muted{color:#7b8b99;font-size:12px}
pre{background:#f7f9fc;border:1px solid var(--line);border-radius:6px;padding:12px;overflow:auto;font-size:12px;white-space:pre-wrap}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}
.kv div:nth-child(odd){color:#5a6b7a}
</style>
</head>
<body>
<header>
  <h1>fintrace 核查台 · 披露材料内部核查</h1>
  <p>原始 PDF → 页码定位 → 确定性复算 → 可追溯报告　（只监听本机；判定全在本地确定性代码，模型只负责讲解）</p>
</header>
<main>
  <section>
    <h2>一、选择材料与口径</h2>
    <div class="grid">
      <div class="wide">
        <label>被核查材料（data/raw 下的 PDF）</label>
        <select id="pdf"></select>
      </div>
      <div><label>预警截止日（YYYY-MM-DD）</label><input id="cutoff" value="2024-04-01"></div>
      <div><label>材料发布日期（YYYY-MM-DD）</label><input id="pub" value="2024-03-29"></div>
      <div><label>主体名称（可空）</label><input id="company" placeholder="启明信息"></div>
      <div><label>案例编号（可空，默认取文件名）</label><input id="caseid" placeholder="qiming-2023-dividend"></div>
      <div class="wide">
        <label>官方更正公告（可选；只做事后比对，不参与判定）</label>
        <select id="notice"></select>
      </div>
    </div>
    <p style="margin:14px 0 0">
      <button id="run">运行核查</button>
      <span id="status" class="muted" style="margin-left:10px"></span>
    </p>
  </section>
  <div id="result"></div>
  <p id="inst" class="muted" style="text-align:center;margin-top:22px"></p>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function loadCases() {
  const r = await fetch("/api/cases");
  const d = await r.json();
  const fill = (sel, withEmpty) => {
    sel.innerHTML = withEmpty ? '<option value="">— 不用 —</option>' : "";
    for (const c of d.cases) {
      const o = document.createElement("option");
      o.value = c.path;
      o.textContent = c.dir + " / " + c.name + "（" + c.size_mb + " MB）";
      sel.appendChild(o);
    }
  };
  fill($("pdf"), false);
  fill($("notice"), true);
  $("status").textContent = d.cases.length ? "找到 " + d.cases.length + " 份 PDF" : "data/raw 下没有 PDF";
}

function render(d) {
  const out = [];
  const c = d.conclusion || {};
  const cls = c.status === "CONSISTENT" ? "ok" : "bad";
  out.push('<section><div class="banner ' + cls + '">' + esc(d.summary) + '　（不一致 ' +
    c.error + " 处 / 可疑 " + c.warn + " 处 / 说明 " + c.info + " 处）</div>");

  const rec = d.recalculation || {};
  out.push('<h2>复算过程</h2><table><tr><th>项</th><th>取值</th><th>出处</th></tr>' +
    "<tr><td>股本基数</td><td>" + esc(rec.shares) + "</td><td>" + esc(rec.shares_source) + "</td></tr>" +
    "<tr><td>每 10 股派息数</td><td>" + esc(rec.dividend_per_ten_shares) + "</td><td>" +
    esc(rec.dividend_per_ten_shares_source) + "</td></tr>" +
    "<tr><td>复算值</td><td>" + esc(rec.expected_yuan) + " 元</td><td>" + esc(rec.formula) + "</td></tr>" +
    "</table>");

  const kv = [];
  kv.push("<div>运行日志编号</div><div>" + esc(d.run_log_id) + "</div>");
  kv.push("<div>材料</div><div>" + esc(d.annual_file) + "（" + d.page_count + " 页）</div>");
  const hits = Object.entries(d.pages_hit || {}).filter(([, v]) => v && v.length)
    .map(([k, v]) => k + " 第 " + v.join("、") + " 页").join("；");
  kv.push("<div>页码定位</div><div>" + esc(hits || "无") + "</div>");
  if (d.notice_check && d.notice_check.hit) {
    kv.push("<div>事后比对</div><div>复算值 " + esc(d.notice_check.value) + " 出现在 " +
      esc(d.notice_check.file) + " 第 " + d.notice_check.page + " 页</div>");
  }
  out.push('<div class="kv" style="margin-top:10px">' + kv.join("") + "</div>");

  out.push("<h2 style='margin-top:18px'>发现清单</h2>");
  if (!(d.findings || []).length) out.push("<p class='muted'>本次没有产出发现。</p>");
  for (const f of d.findings || []) {
    out.push('<div class="finding ' + esc(f.severity) + '">' +
      '<div><span class="' + esc(f.severity) + '-t">[' + esc(f.severity) + "] " + esc(f.title) + "</span> " +
      '<span class="code">' + esc(f.code) + "</span></div>" +
      "<div>" + esc(f.detail) + "</div>" +
      (f.evidence || []).map((e) => '<div class="ev">· ' + esc(e.file) + " 第 " + esc(e.page) +
        " 页：" + esc(e.line) + (e.value ? "　值 = " + esc(e.value) : "") + "</div>").join("") +
      "</div>");
  }
  if (d.saved && d.saved.report) {
    out.push('<p class="muted" style="margin-top:10px">已写出：' + esc(d.saved.report) + "　|　" +
      esc(d.saved.payload) + "　（耗时 " + d.elapsed_ms + " ms）</p>");
  }
  out.push('<h2 style="margin-top:18px">报告全文（markdown）</h2><pre>' + esc(d.markdown) + "</pre>");
  out.push('<section><h2 style="margin-top:18px">AI 解释（大模型，非判定环节）</h2>' +
    '<p style="margin:6px 0 8px"><button id="explain">让 AI 讲成人话</button> ' +
    '<span id="ai_meta" class="muted"></span></p>' +
    '<pre id="ai_text" class="muted">点上面按钮生成：确定性结果照旧，模型只负责把它讲成人话。</pre></section>');
  out.push("</section>");
  $("result").innerHTML = out.join("");
  $("explain").addEventListener("click", explain);
}

async function run() {
  $("run").disabled = true;
  $("status").textContent = "正在读 PDF 并复算…";
  $("result").innerHTML = "";
  try {
    const r = await fetch("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        pdf: $("pdf").value, notice: $("notice").value,
        cutoff: $("cutoff").value, publication_date: $("pub").value,
        company: $("company").value, case_id: $("caseid").value
      })
    });
    const d = await r.json();
    if (!d.ok) {
      $("result").innerHTML = '<section><div class="banner fail">运行失败：' + esc(d.error) + "</div></section>";
      $("status").textContent = "失败";
    } else {
      render(d);
      $("status").textContent = "完成（耗时 " + d.elapsed_ms + " ms）";
    }
  } catch (err) {
    $("result").innerHTML = '<section><div class="banner fail">请求出错：' + esc(err) + "</div></section>";
    $("status").textContent = "出错";
  } finally {
    $("run").disabled = false;
  }
}

async function explain() {
  const btn = $("explain");
  btn.disabled = true;
  $("ai_meta").textContent = "正在调大模型…";
  $("ai_text").textContent = "";
  try {
    const r = await fetch("/api/explain", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        pdf: $("pdf").value, notice: $("notice").value,
        cutoff: $("cutoff").value, publication_date: $("pub").value,
        company: $("company").value, case_id: $("caseid").value,
      }),
    });
    const d = await r.json();
    if (!d.ok) {
      $("ai_meta").textContent = "";
      $("ai_text").textContent = "生成失败：" + d.error;
      return;
    }
    $("ai_meta").textContent = "模型 " + d.model + "　key " + (d.key_hint || "—") +
      "　耗时 " + d.elapsed_ms + " ms　（只做叙述；判定仍由确定性代码完成）";
    $("ai_text").textContent = d.text;
  } catch (e) {
    $("ai_text").textContent = "请求失败：" + e;
  } finally {
    btn.disabled = false;
  }
}

$("run").addEventListener("click", run);
loadCases();
fetch("/api/health").then((r) => r.json()).then((h) => {
  $("inst").textContent = "本机核验台实例 pid " + h.pid + "　启动于 " + h.started_at +
    "　（同时开两个实例会抢连接，若结果像是旧的，先确认 pid）";
});
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- 后端


def _allowed_pdf(raw: str) -> Path:
    """解析并校验材料路径，只放行 ALLOWED_ROOTS 下的 PDF。"""
    if not raw:
        raise ValueError("没有选择材料")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if not p.exists():
        raise ValueError(f"材料不存在：{p}")
    roots = [r.resolve() for r in ALLOWED_ROOTS]
    if not any(str(p).startswith(str(r)) for r in roots):
        raise ValueError("只允许核查 data/raw 与 data/uploads 下的 PDF")
    if p.suffix.lower() != ".pdf":
        raise ValueError("只支持 PDF 材料")
    return p


def list_cases() -> list[dict]:
    """列出允许核查的 PDF。"""
    out: list[dict] = []
    for root in ALLOWED_ROOTS:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.pdf")):
            rel = f.relative_to(ROOT)
            out.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "name": f.name,
                    "dir": str(rel.parent).replace("\\", "/"),
                    "size_mb": round(f.stat().st_size / 1048576, 2),
                }
            )
    return out


def run_case(payload: dict) -> dict:
    """执行一次核查，返回给前端的 JSON。"""
    t0 = time.time()
    annual = _allowed_pdf(payload.get("pdf", ""))
    notice = _allowed_pdf(payload["notice"]) if payload.get("notice") else None

    cutoff = (payload.get("cutoff") or "").strip()
    pub = (payload.get("publication_date") or "").strip()
    if not cutoff or not pub:
        raise ValueError("预警截止日与材料发布日期都必须填（YYYY-MM-DD）")

    case_id = (payload.get("case_id") or "").strip() or annual.stem

    res = dividend_case.run_pdf_case(
        annual,
        cutoff_date=cutoff,
        publication_date=pub,
        company=(payload.get("company") or "").strip(),
        case_id=case_id,
        notice_path=notice,
        outdir=OUT_ROOT / case_id,
    )
    data = res.to_dict()
    data["summary"] = res.summary_line()
    data["markdown"] = res.markdown
    data["elapsed_ms"] = int((time.time() - t0) * 1000)
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "fintrace-ui"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/cases":
            self._json({"ok": True, "cases": list_cases()})
        elif path == "/api/llm":
            self._json({"ok": True, **llm.public_config()})
        elif path == "/api/health":
            self._json(
                {"ok": True, "root": str(ROOT), "pid": os.getpid(), "started_at": STARTED_AT}
            )
        else:
            self._json({"ok": False, "error": "未知路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/explain"):
            self._json({"ok": False, "error": "未知路径"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return
        try:
            data = run_case(payload)
        except Exception as exc:  # 材料读不了、日期非法、缺字段等，回给前端显示
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/run":
            self._json({"ok": True, **data})
            return
        # /api/explain：确定性结果原样给回，模型只负责讲成人话
        cfg = llm.load_config()
        view = llm.public_config(cfg)
        started = time.time()
        try:
            text = llm.explain(
                annual_file=data["annual_file"],
                meta={"复算过程": data["recalculation"], "页码定位": data["pages_hit"]},
                findings=data["findings"],
                report_markdown=data["markdown"],
                cfg=cfg,
            )
        except llm.LLMError as exc:
            self._json({"ok": False, "error": str(exc), "has_key": view["has_key"]})
            return
        self._json({
            "ok": True,
            "model": view["model"],
            "key_hint": view["key_hint"],
            "text": text,
            "elapsed_ms": int((time.time() - started) * 1000),
        })

    def log_message(self, fmt: str, *args) -> None:
        print("[ui] " + fmt % args)


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """启动核验台（阻塞）。"""
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"核查台已启动：{url}　（pid {os.getpid()}，启动于 {STARTED_AT}）")
    print(f"可核查的材料目录：{', '.join(str(r.relative_to(ROOT)) for r in ALLOWED_ROOTS)}")
    print("（Ctrl+C 停止；只监听本机，不联网也能跑）")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # 打不开浏览器不影响服务
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
