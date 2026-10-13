"""把 `~/api-key` 里的 key 装进本项目 `.secrets/llm.json`（**明文永不打印**）。

key 文件里一行一条，形如 `标签：sk-xxxx` 或单独一行 `sk-xxxx`。

```bash
python tools/use_key.py                      # 取「金融智能体」那一行（本项目专用）
python tools/use_key.py --label 手机操作      # 取别的条目
python tools/use_key.py --source <文件> --line 1
python tools/use_key.py --show               # 只看当前配置（掩码）
python tools/use_key.py --model deepseek-reasoner --base-url https://api.deepseek.com/v1
```

输出只有掩码（`sk-1***e828`），不打印明文；文件按 0600 写，且 `.secrets/` 已在 `.gitignore` 里。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets" / "llm.json"
DEFAULT_SOURCE = Path.home() / "api-key" / "deepseek" / "Token.txt"
DEFAULT_LABEL = "金融智能体"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")


def mask(key: str) -> str:
    if not key:
        return "(空)"
    return "***" if len(key) <= 8 else f"{key[:4]}***{key[-4:]}"


def parse_entries(path: Path) -> list[tuple[str, str]]:
    """返回 [(标签, key)]；没标签的条目标签为空串。"""
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _KEY_RE.search(line)
        if not m:
            continue
        label = line[: m.start()].strip(" \t：:=-—　")
        out.append((label, m.group(0)))
    return out


def pick(entries: list[tuple[str, str]], *, label: str, line: int) -> tuple[str, str]:
    if line:
        if not (1 <= line <= len(entries)):
            raise SystemExit(f"文件里只有 {len(entries)} 条 key，取不到第 {line} 条")
        return entries[line - 1]
    for lab, key in entries:
        if label and label in lab:
            return lab, key
    raise SystemExit(
        "没找到标签含「%s」的条目。文件里现有的条目：%s"
        % (label, "、".join(lab or "(无标签)" for lab, _ in entries))
    )


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="装 key 进 .secrets/llm.json")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--label", default=DEFAULT_LABEL)
    ap.add_argument("--line", type=int, default=0, help="直接指定第几行（从 1 数）")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--show", action="store_true", help="只看当前配置，不写入")
    args = ap.parse_args(argv)

    cur = load_json(SECRETS)

    if args.show:
        print(f"配置文件：{SECRETS}（{'存在' if SECRETS.is_file() else '不存在'}）")
        print(f"  api_key：{mask(str(cur.get('api_key') or ''))}")
        print(f"  base_url：{cur.get('base_url') or DEFAULT_BASE_URL}")
        print(f"  model：{cur.get('model') or DEFAULT_MODEL}")
        return 0

    if not args.source.is_file():
        print(f"找不到 key 文件：{args.source}", file=sys.stderr)
        return 2
    entries = parse_entries(args.source)
    if not entries:
        print(f"{args.source} 里没找到 sk- 形态的 key", file=sys.stderr)
        return 2

    lab, key = pick(entries, label=args.label, line=args.line)
    payload = {
        "api_key": key,
        "base_url": args.base_url or str(cur.get("base_url") or DEFAULT_BASE_URL),
        "model": args.model or str(cur.get("model") or DEFAULT_MODEL),
    }
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:                                                         # 只给自己读写
        os.chmod(SECRETS, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass

    print(f"已写入 {SECRETS}")
    print(f"  采用条目：{lab or '(无标签)'}　（来源 {args.source}，共 {len(entries)} 条）")
    print(f"  api_key：{mask(key)}（{len(key)} 位）")
    print(f"  base_url：{payload['base_url']}")
    print(f"  model：{payload['model']}")
    print(f"  权限：{oct(SECRETS.stat().st_mode)[-3:]}　已 gitignore：是")
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
