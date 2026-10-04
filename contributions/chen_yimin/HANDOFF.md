# 陈翊民：确定性计算模块交付

这是此前本地开发版本的独立交付包，尚未替换仓库主流程，也不代表团队已决定竞赛定位。仓库根目录的 AGENTS.md、STATUS.md 与统一 ClaimRecord 接口仍有效。本目录 README 和 docs 保留开发阶段说明，其中定位、状态和本机路径不能视为全队最新决策。

## 在独立进程验证

从仓库根目录进入本目录，再执行：

```powershell
cd contributions/chen_yimin
$env:PYTHONPATH="src"
python -m unittest discover -s tests -q
python -m fintrace.cli demo
```

本目录与根工程均使用 fintrace 包名，必须在不同进程、不同 PYTHONPATH 下运行，暂勿同时安装两个包。根项目回归仍在仓库根执行 `python -m pytest tests -q`。

## 本次交付

- Decimal 金额计算、单位换算、材料声明复核。
- 同行中位数、MAD、经验分位位置；样本不足和MAD为零显式返回不可用字段。
- 同行逐条准入、重复公司排除、目标公司排除、非有限数值拦截、来源证据回传。
- 美晨开发案例的已整理结构化记录；不是重新核验的正式评测集。来源日期尚缺，因此不应宣称完成真实事前验证。
- peer_guard 测试内 synthetic 数据仅用于程序测试，不是实际同行。

## 谷纪瑄对接顺序

1. 优先将 thresholds.py 的纯计算函数迁入主工程 calculators；复算结果转换为已有 RecomputeResult，保留数值精度约定。
2. 所有主流程取数仍经过 verification/cutoff_filter.py；本交付包的 peer_guard 只能作为同行附加检查，不是替代入口。
3. 将文件、页码、日期映射到 EvidenceRef；核验标记是人工登记，程序未自动阅读PDF验证真实性。
4. 核对 scope / definition_version / peer_group 是否与目标指标真实一致，不能只让同行彼此一致。
5. 显示 accepted_records / excluded_records；AVAILABLE 仅表示计算条件满足，不代表可靠预警。

## 已知边界

- 计算仍固定2019/2020，通用年度支持未完成。
- 基期利润为负的增速解释仍需处理；不能直接解释正增速为改善。
- 主案例来源日期缺失时本包仍警告后计算；主工程应保持缺日期不可用的严格规则。
- 10家只是可配置项目门槛，不能当统计可靠性保证；避免为凑样本引入不可比公司。
- 当前仅交付源码与结构化数据，未上传原始PDF、Excel、密钥或运行产物。
