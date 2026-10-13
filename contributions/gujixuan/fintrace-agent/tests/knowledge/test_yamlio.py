"""极简 YAML 读写器的 round-trip（它替代 PyYAML，必须自证）。"""
from __future__ import annotations

from fintrace_agent.knowledge import yamlio


def _roundtrip(obj):
    text = yamlio.dumps(obj)
    return yamlio.loads(text), text


def test_scalars_and_types():
    obj = {
        "s": "营业收入 42.02 亿元",
        "i": 7,
        "f": 10000.0,
        "t": True,
        "f2": False,
        "n": None,
        "empty_s": "",
    }
    got, _ = _roundtrip(obj)
    assert got == obj


def test_nested_structures():
    obj = {
        "schema_version": 1,
        "records": [
            {"unit": "%", "base": "%", "scale": 1.0, "precision": 2},
            {"unit": "元", "base": "元", "scale": 1.0, "precision": 2},
        ],
        "matrix": [[1, 2], [3, 4]],
        "empty_list": [],
        "empty_dict": {},
    }
    got, _ = _roundtrip(obj)
    assert got == obj


def test_quotes_and_specials_roundtrip():
    obj = {"cn": '含"引号"与\\反斜杠与\n换行', "list": ["a:b", "-x", "1.23"]}
    got, _ = _roundtrip(obj)
    assert got == obj


def test_expected_text_shape():
    text = yamlio.dumps({"a": 1, "b": ["x", "y"]})
    assert '"a": 1' in text
    assert '- "x"' in text
    assert '- "y"' in text
