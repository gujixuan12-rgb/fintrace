# 环境与验收注意事项（踩过的坑，别再踩一遍）

## 1. 本机 pytest 的汇总行会被丢掉

重定向输出时，`pytest` 的末行汇总（`185 passed in ...`）在本机读不到，日志里只有进度点。

验收判定因此固定为两条：**看退出码**，加上**数进度点**（exit=0 时点即通过数，`s` 是跳过）。
例如本机全量（2026-10-10）：进度点 357、跳过 0、`exit=0`。

## 2. `pythonpath = ["src"]` 只在 pytest 内生效

`pyproject.toml` 里的 `[tool.pytest.ini_options] pythonpath` 由 pytest 注入，pytest 之外跑
临时脚本要自己带路径：

```bash
PYTHONPATH=src .venv/Scripts/python.exe -c "import fintrace_agent; print('ok')"
```

不带就会 `ModuleNotFoundError: No module named 'fintrace_agent'` —— 那是调用方式问题，
不是代码问题（已实际踩过一次，白查一轮）。

## 3. 多目录套件里禁止裸名导入 conftest

pytest 会把每个测试目录都插进 `sys.path`，`from conftest import ...` 命中的是**先被导入的那个**
conftest，取决于收集顺序：全量能过、单跑某个目录就 `ImportError`，CI 换个顺序会突然红。

规矩：公共构造器放**名字唯一**的模块（本项目用的是 `tests/facts/facts_factories.py`、
`tests/gate/gate_factories.py`）并从这里导入；`conftest.py` 只放夹具。

两个目录都有 `factories.py` 时，`import factories` 仍会撞车 —— 模块名必须全局唯一。

## 4. 验收命令一律不要带管道

`pytest ... | tail` 之后 `$?` 取的是 `tail` 的退出码，会给出假的通过。
固定写法：

```bash
.venv/Scripts/python.exe -m pytest tests -q > "$TMPDIR/x.log" 2>&1; echo "exit=$?"
```

## 5. 引文回查要按片段做，不要整句比

内核给出的证据引文是**单元格片段**，形如 `营业收入 / （元） / 1,243,18`（用「/」分隔，
数值可能是截断的）。拿它跟原页文本做整句子串匹配会全军覆没：固定链路 15 条结论
被 V 门拦掉 14 条，全是 `V2: 引文与原件对不上`。

正确口径：**按「/」「|」切段，每个实质片段都要能在原件里找到**（纯单位/符号段跳过）；
数字片段按**数字边界**核对，`27,116,21` 不算命中 `27,116,214.63`（截断的数字核验不了就该拦）。
片段形态只记提示，不阻断。

## 6. 指标分母必须按模式分离

六项新指标只在 `mode == "agent"` 的运行上算。把固定链路的基线运行混进分母，
读数会被稀释成没有意义的数字（实测踩过：ASR 算成 `0/5`）。报告里要写出排除了几个基线运行。

## 7. 类体里取不到外层函数的局部变量

```python
def f(claims):
    class C:            # 类作用域不查函数局部
        data = list(claims)   # NameError
```
要动态造对象就用 `types.SimpleNamespace(...)`，别用「函数里定义 class」这种写法
（踩过一次：回路拿不到主张列表，报 NameError）。

## 8. 解释器与依赖

项目自带虚拟环境 `.venv`（Python 3.12.10，已装 pytest 9.1.1、pymupdf 1.28.2）。
团队仓库那边用的是 Hermes 的 venv，两者不要混；本项目的所有命令都用
`G:/项目/fintrace-agent/.venv/Scripts/python.exe`。
