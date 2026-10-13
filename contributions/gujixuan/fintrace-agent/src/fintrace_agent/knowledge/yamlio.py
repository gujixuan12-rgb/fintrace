"""极简 YAML 读写（仅覆盖本项目知识数据文件用到的子集）。

取舍说明（BUILD_SPEC 第 0 节不允许新增依赖，而 YAML 不在标准库里）：
- 方案一：改用 JSON 存数据文件 —— 但 BUILD_SPEC 第 2 节与交付要求都写明文件名是
  ``{concepts,units,formulas,contexts,relations}.yaml``，改成 .json 会与规格对不上。
- 方案二（本实现采用）：自己写一个**受控的极小子集** YAML 读写器，保持 .yaml 文件名。
  发射与解析互为逆运算，round-trip 由测试钉住（``tests/knowledge/test_yamlio.py``）。

支持的范围：
- 块状映射（``"key": value``，键一律用双引号）；
- 块状序列（``- value`` / ``- "key": value``）；
- 标量：字符串（一律 JSON 双引号转义，非 ASCII 原样保留）、整数、浮点、``true``/``false``、``null``。
不支持：锚点/别名、多文档、流式集合、折叠块、注释、自定义标签 —— 本项目数据文件用不到。
"""
from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# 发射
# ---------------------------------------------------------------------------


def _emit_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"无法序列化非有限浮点数: {value!r}")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"极简 YAML 不支持的类型: {type(value).__name__}")


def _dump(value: Any, indent: int) -> list[str]:
    ind = " " * indent
    if isinstance(value, dict):
        if not value:
            return ["{}"]
        lines: list[str] = []
        for key in sorted(value.keys()):
            key_s = json.dumps(str(key), ensure_ascii=False)
            v = value[key]
            if isinstance(v, dict) and v:
                lines.append(f"{ind}{key_s}:")
                lines.extend(_dump(v, indent + 2))
            elif isinstance(v, list) and v:
                lines.append(f"{ind}{key_s}:")
                lines.extend(_dump(v, indent + 2))
            elif isinstance(v, dict):
                lines.append(f"{ind}{key_s}: {{}}")
            elif isinstance(v, list):
                lines.append(f"{ind}{key_s}: []")
            else:
                lines.append(f"{ind}{key_s}: {_emit_scalar(v)}")
        return lines
    if isinstance(value, list):
        if not value:
            return ["[]"]
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                sub = _dump(item, indent + 2)
                # sub[0] 形如 "<indent+2>个空格 + key: ..."，把它挪到 "- " 之后
                lines.append(f"{ind}- {sub[0][indent + 2:]}")
                lines.extend(sub[1:])
            elif isinstance(item, list) and item:
                lines.append(f"{ind}-")
                lines.extend(_dump(item, indent + 2))
            elif isinstance(item, dict):
                lines.append(f"{ind}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{ind}- []")
            else:
                lines.append(f"{ind}- {_emit_scalar(item)}")
        return lines
    return [ind + _emit_scalar(value)]


def dumps(value: Any) -> str:
    """把 dict / list / 标量序列化成极简 YAML 文本（结尾带换行）。"""
    return "\n".join(_dump(value, 0)) + "\n"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _end_of_quoted(s: str, i: int) -> int:
    """``s[i]`` 是开引号，返回闭引号的下标（处理反斜杠转义）。"""
    assert s[i] == '"'
    j = i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == '"':
            return j
        j += 1
    raise ValueError(f"字符串未闭合: {s!r}")


def _is_mapping_line(s: str) -> bool:
    if not s.startswith('"'):
        return False
    j = _end_of_quoted(s, 0)
    return s[j + 1:].lstrip().startswith(":")


def _split_key(s: str) -> tuple[str, str]:
    j = _end_of_quoted(s, 0)
    key = json.loads(s[: j + 1])
    rest = s[j + 1:].strip()
    if not rest.startswith(":"):
        raise ValueError(f"不是映射行: {s!r}")
    return key, rest[1:].strip()


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith('"'):
        return json.loads(s)
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    raise ValueError(f"无法解析标量: {s!r}")


def loads(text: str) -> Any:
    """解析 :func:`dumps` 产生的极简 YAML 文本。"""
    items: list[tuple[int, str]] = []
    for raw in text.splitlines():
        st = raw.strip()
        if not st:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        items.append((indent, st))
    if not items:
        return None
    if len(items) == 1 and items[0][1] in ("{}", "[]"):
        return {} if items[0][1] == "{}" else []

    pos = [0]

    def parse_block(indent: int) -> Any:
        ind0, s0 = items[pos[0]]
        if s0 == "-" or s0.startswith("- "):
            return parse_seq(indent)
        return parse_map(indent)

    def parse_seq(indent: int) -> list:
        out: list = []
        while pos[0] < len(items):
            ind, s = items[pos[0]]
            if ind != indent or not (s == "-" or s.startswith("- ")):
                break
            content = s[1:].strip()
            if content == "":
                pos[0] += 1
                if pos[0] < len(items) and items[pos[0]][0] > indent:
                    out.append(parse_block(items[pos[0]][0]))
                else:
                    out.append(None)
            elif _is_mapping_line(content):
                # 该项是映射，首个键跟着 "- "；整体映射缩进比 "- " 多 2
                items[pos[0]] = (indent + 2, content)
                out.append(parse_map(indent + 2))
            elif content == "{}":
                pos[0] += 1
                out.append({})
            elif content == "[]":
                pos[0] += 1
                out.append([])
            else:
                pos[0] += 1
                out.append(_parse_scalar(content))
        return out

    def parse_map(indent: int) -> dict:
        out: dict = {}
        while pos[0] < len(items):
            ind, s = items[pos[0]]
            if ind != indent or s == "-" or s.startswith("- "):
                break
            if not _is_mapping_line(s):
                raise ValueError(f"期望映射行: {s!r}")
            key, rest = _split_key(s)
            if rest == "":
                pos[0] += 1
                if pos[0] < len(items) and items[pos[0]][0] > indent:
                    out[key] = parse_block(items[pos[0]][0])
                else:
                    out[key] = None
            elif rest == "{}":
                pos[0] += 1
                out[key] = {}
            elif rest == "[]":
                pos[0] += 1
                out[key] = []
            else:
                pos[0] += 1
                out[key] = _parse_scalar(rest)
        return out

    return parse_block(items[0][0])


__all__ = ["dumps", "loads"]
