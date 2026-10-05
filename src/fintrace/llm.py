"""大模型层：把「材料 → 可核验断言」的拆解和「核查结果 → 人话」的解释交给模型。

判定纪律不变（这是本项目的硬边界）：

* 模型**不参与任何数值判定**。复算与一致性判定仍由 `calculators/`、`verification/`
  里的确定性代码完成，模型看不到也改不了那一步的结果。
* 模型**不许引入给定材料之外的事实或数字**，每条输出都要能指回原文出处。
* 模型**不许下定性结论**（造假、虚增、舞弊、爆雷一类措辞一律禁止）。

接入方式：DeepSeek 的 OpenAI 兼容接口（`POST {base_url}/chat/completions`），
只用标准库 `urllib`，不给主仓库引入任何第三方依赖。

配置来源（优先级从高到低）：

1. 环境变量 `FINTRACE_LLM_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
2. 本机 `.secrets/llm.json`（已 gitignore）
3. 项目根 `.env`（已 gitignore）

没配置时所有函数抛 `LLMError`，调用方应当把它当成「这个功能暂时不可用」，
而不是让整条核查链路失败 —— 确定性部分照常出结果。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / ".secrets" / "llm.json"
ENV_FILE = ROOT / ".env"

DEFAULTS: dict = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
    "timeout": 60,
}

_ENV_KEYS = ("FINTRACE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


class LLMError(RuntimeError):
    """没配置、网络不通、接口报错 —— 都归到这里，方便调用方降级处理。"""


# --------------------------------------------------------------------- 配置


def _read_env_file() -> dict:
    if not ENV_FILE.exists():
        return {}
    found: dict = {}
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        found[name.strip()] = value.strip().strip('"').strip("'")
    return found


def load_config() -> dict:
    """读本机配置；环境变量优先于文件里的 key。"""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except json.JSONDecodeError:
            pass
    env_file = _read_env_file()
    for name in _ENV_KEYS:
        value = os.environ.get(name) or env_file.get(name)
        if value:
            cfg["api_key"] = value
            cfg["_key_from"] = name
            break
    return cfg


def mask(key: str) -> str:
    """掩码：给界面看的，明文永不出后端。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "*" * max(len(key) - 2, 0)
    return f"{key[:5]}…{key[-4:]}"


def public_config(cfg: dict | None = None) -> dict:
    """安全视图：只说有没有配、掩码是什么，绝不回明文。"""
    cfg = cfg or load_config()
    key = cfg.get("api_key") or ""
    return {
        "base_url": cfg.get("base_url", DEFAULTS["base_url"]),
        "model": cfg.get("model", DEFAULTS["model"]),
        "has_key": bool(key),
        "key_hint": mask(key),
        "key_from": cfg.get("_key_from", "本机配置" if key else ""),
    }


def available(cfg: dict | None = None) -> bool:
    return bool((cfg or load_config()).get("api_key"))


# --------------------------------------------------------------------- 调用


def chat(messages: list[dict], *, cfg: dict | None = None, json_mode: bool = False,
         temperature: float = 0.2) -> str:
    """底层调用，OpenAI 兼容格式。返回模型输出的文本（json_mode 时是 JSON 字符串）。"""
    cfg = cfg or load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise LLMError(
            "还没有配置大模型 API Key。把 key 放进 .secrets/llm.json，"
            "或设成环境变量 DEEPSEEK_API_KEY / FINTRACE_LLM_API_KEY。"
        )
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMError("base_url 是空的，填成 https://api.deepseek.com 这类地址。")

    body: dict = {
        "model": cfg.get("model") or DEFAULTS["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 60))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover
            pass
        raise LLMError(f"接口返回 {exc.code}：{detail or exc.reason}") from exc
    except Exception as exc:
        raise LLMError(f"调用失败：{type(exc).__name__} {exc}") from exc

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"返回结构不是预期的 OpenAI 兼容格式：{str(data)[:200]}") from exc


# ----------------------------------------------------------------- 提示词


_EXPLAIN_SYSTEM = """你是上市公司披露材料的核查助手。你会收到一份**确定性核查结果**（规则已经算完），
你的任务只是把它翻译成人话，并给出可执行的修改建议。

必须遵守：
1. 不得下定性结论。禁止出现“造假/舞弊/虚增/隐瞒/财务造假/爆雷”等判断性措辞；
   只说“披露之间存在不一致”“某处数值与复算结果不符”。
2. 不得引入核查结果之外的事实或数字。你写的每个数字都要能在给的证据里找到。
3. 每条解释都要引用具体出处（文件名 + 页码 + 原文行）。
4. 材料里没写、取不到的东西，直接说“未取到 / 无法判断”，不要补。
5. 输出用简体中文，分三节：一、问题说明（每条一段，通俗讲清错在哪）；
   二、修改建议（指出应改成什么、依据是什么）；三、复核要点（人工需要确认什么）。
简洁，不要客套，不要复述整张清单。"""

_DECOMPOSE_SYSTEM = """你是财报与研报核查的前处理助手。你会收到一段材料原文，
把它拆成**一条条可核验的断言**。每条断言必须满足：

1. 可核验：含有具体数值、比率、时间或主体，能拿去和披露原文逐项对照。
2. 忠于原文：不得加入原文没有的数字或判断；数值原样抄录，不要换算。
3. 带出处：给出该断言所在页码与原文片段；定位不到就把 page 留空、locatable 置 false，
   并在 reason 里说清为什么定位不到。
4. 措辞中立：判断性措辞（造假、虚增、舞弊）不要写进 claim_text，
   改写成“某处披露与计算值不一致”这类可核验表述。

只输出 JSON，结构：
{"claims": [{"claim_text": "...", "metric": "...", "value": "...", "unit": "...",
  "period": "...", "subject": "...", "page": 0, "quoted_text": "...",
  "locatable": true, "reason": ""}]}
没拆出任何断言时输出 {"claims": []}。不要输出 JSON 之外的任何文字。"""


# --------------------------------------------------------------------- 能力


def explain(*, annual_file: str, meta: dict, findings: list[dict],
            report_markdown: str, cfg: dict | None = None) -> str:
    """把确定性核查结果讲成人话。模型只做叙述，不改判定。"""
    payload = json.dumps(
        {
            "被核查材料": annual_file,
            "复算过程": meta,
            "发现清单": [
                {
                    "code": f.get("code"),
                    "severity": f.get("severity"),
                    "title": f.get("title"),
                    "detail": f.get("detail"),
                    "evidence": f.get("evidence"),
                }
                for f in findings
            ],
            "完整报告": report_markdown,
        },
        ensure_ascii=False,
        indent=2,
    )
    return chat(
        [
            {"role": "system", "content": _EXPLAIN_SYSTEM},
            {"role": "user", "content": payload},
        ],
        cfg=cfg,
    )


def decompose_claims(text: str, *, source: str = "", cfg: dict | None = None) -> list[dict]:
    """把一段材料拆成可核验断言。返回列表；模型没拆出东西时返回空列表。"""
    if not text.strip():
        raise LLMError("没有可拆解的材料正文。")
    user = (f"材料来源：{source}\n\n" if source else "") + text
    raw = chat(
        [
            {"role": "system", "content": _DECOMPOSE_SYSTEM},
            {"role": "user", "content": user},
        ],
        cfg=cfg,
        json_mode=True,
        temperature=0.0,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型没有按 JSON 返回：{raw[:200]}") from exc
    claims = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(claims, list):
        raise LLMError(f"返回结构里没有 claims 列表：{str(parsed)[:200]}")
    return [c for c in claims if isinstance(c, dict) and str(c.get("claim_text", "")).strip()]
