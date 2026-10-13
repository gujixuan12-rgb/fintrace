"""模型客户端与决策解析（对应 BUILD_SPEC §8 · v2.2 §4）。

两条纪律：
1. **key 不进代码 / 日志 / 异常文本**；日志里只准出现掩码。本模块提供 ``mask_key``。
2. **未配置时明确失败**（``NotConfigured``），绝不静默假装成功；回路据此回落 fixed。

key 读取顺序：环境变量 ``FINTRACE_LLM_API_KEY`` → ``DEEPSEEK_API_KEY`` → ``OPENAI_API_KEY``
→ ``.secrets/llm.json``（可选 ``api_key`` / ``base_url`` / ``model`` 三键）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_TIMEOUT = 60

#: 环境变量读取顺序（谁在前谁优先）
_ENV_KEYS = ("FINTRACE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
_ENV_BASEURL = "FINTRACE_LLM_BASE_URL"
_ENV_MODEL = "FINTRACE_LLM_MODEL"

#: 允许的动作白名单从契约取，避免两处定义
from ..contracts import ACTIONS  # noqa: E402


class NotConfigured(RuntimeError):
    """模型未配置（无 key 或无 model）—— 明确失败，绝不发请求。"""


class DecisionError(ValueError):
    """决策文本非法（不是严格 JSON / 缺字段）。``str(e)`` 里不含任何 key。"""


def mask_key(key: str) -> str:
    """key 的掩码：只留首尾各 4 位；短 key 一律 ``***``。用于日志。"""
    k = key or ""
    if len(k) <= 8:
        return "***" if k else ""
    return f"{k[:4]}...{k[-4:]}"


def _secrets_path():
    # 延迟取路径，测试可 monkeypatch paths.SECRETS
    from .. import paths
    return paths.SECRETS


def _load_secrets() -> dict:
    """读 ``.secrets/llm.json``；不存在或坏了都返回 {}（不抛）。"""
    try:
        path = _secrets_path()
        if not path or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_key() -> str:
    """按顺序解析 key：环境变量优先，其次 ``.secrets/llm.json``。找不到返回空串。"""
    for name in _ENV_KEYS:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    sec = _load_secrets()
    return str(sec.get("api_key") or "").strip()


class ModelClient:
    """OpenAI 兼容的 chat-completions 客户端（只用标准库 ``urllib``）。"""

    def __init__(self, *, base_url: str = "", model: str = "", api_key: str = "",
                 timeout: int = DEFAULT_TIMEOUT):
        sec = _load_secrets()
        self.base_url = (base_url or os.environ.get(_ENV_BASEURL, "")
                         or str(sec.get("base_url") or "") or DEFAULT_BASE_URL).rstrip("/")
        self.model = (model or os.environ.get(_ENV_MODEL, "")
                      or str(sec.get("model") or "")).strip()
        self._api_key = (api_key or resolve_key()).strip()
        self.timeout = int(timeout)

    @property
    def configured(self) -> bool:
        """有 key 且有 model 才算配置好。"""
        return bool(self._api_key and self.model)

    # -- 调用 ---------------------------------------------------------------

    def decide(self, context: dict) -> tuple[str, dict]:
        """把上下文发给模型，返回 ``(原文响应, usage)``。

        未配置 → ``NotConfigured``。异常文本里只出现掩码，绝不出现 key 明文。
        """
        if not self.configured:
            raise NotConfigured(
                f"模型未配置（key={mask_key(self._api_key) or '缺失'}, model={self.model!r}）")

        messages = [
            {"role": "system", "content": _build_system_prompt(context)},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        payload = json.dumps({"model": self.model, "messages": messages,
                              "temperature": 0, "response_format": {"type": "json_object"}}
                             ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:                       # pragma: no cover - 网络
            raise RuntimeError(f"模型 HTTP 错误 {e.code}") from None
        except urllib.error.URLError as e:                        # pragma: no cover - 网络
            raise RuntimeError(f"模型连接失败：{e.reason}") from None
        text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}
        return text, usage

    def explain(self, payload: dict) -> str:
        """让模型用自然语言解释一段载荷（用于报告措辞，非核心链路）。"""
        if not self.configured:
            raise NotConfigured("模型未配置，无法 explain")
        text, _ = self.decide({"task": "explain", "payload": payload})
        return text


def _build_system_prompt(context: dict) -> str:
    contract = (context or {}).get("output_contract", "")
    return (
        "你是受约束的核查 Agent。每轮只能输出一个严格 JSON 对象，形如 "
        '{"action": "...", "args": {...}, "why": "..."}。'
        f"可用动作：{', '.join(ACTIONS)}。禁止输出 JSON 以外任何文字。"
        + (f"\n{contract}" if contract else "")
    )


def parse_decision(text: str) -> dict:
    """严格解析决策 JSON。非法抛 ``DecisionError``（消息里附一段响应片段，不含 key）。"""
    raw = (text or "").strip()
    if not raw:
        raise DecisionError("决策为空")
    # 容忍模型偶尔用 ```json 包裹
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DecisionError(f"非法 JSON：{e.msg}（原文片段：{text.strip()[:120]!r}）") from None
    if not isinstance(obj, dict):
        raise DecisionError(f"决策必须是 JSON 对象，实际是 {type(obj).__name__}")
    if not str(obj.get("action") or "").strip():
        raise DecisionError("决策缺少 action 字段")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise DecisionError("args 必须是对象")
    return {"action": str(obj["action"]).strip(), "args": args,
            "why": str(obj.get("why") or "")}


__all__ = ["ModelClient", "NotConfigured", "DecisionError", "parse_decision",
           "mask_key", "resolve_key", "DEFAULT_BASE_URL"]
