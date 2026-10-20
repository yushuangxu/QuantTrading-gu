# 离线测试

推荐使用现有统一环境：Python3.11.15、NumPy2.4.6、pandas3.0.5、Flask3.1.3。依赖清单为项目根目录`requirements-test.txt`，与生产依赖分离。本次没有安装或修改环境。新机器可在独立测试环境中安装该清单；不要为了运行测试升级生产环境。

项目根目录执行：

```bash
# 日常回归：当前应170项通过、退出0。
/opt/miniconda3/envs/quant/bin/python -B run_tests.py regression

# 已知缺陷：当前应9项普通断言失败、退出1；不是运行成功的退出码。
/opt/miniconda3/envs/quant/bin/python -B run_tests.py known
```

其他依赖齐全的同版本Python可替换绝对解释器路径。入口会打印实际版本；没有依赖会直接失败，不借用其他环境、不自动安装、不跳过测试。

可选完整审计：`python -B run_tests.py all`，当前179项中170通过、9失败，退出1。入口只发现`tests/test_*.py`，不发现根目录的手动联网或历史个股脚本。

`known_issues.json`仅列原9项尚未修复的算法断言；日常回归是全部测试与这9项的严格差集。清单不满9项、有重复、引用不存在的测试或测试身份重复都会报错。新增测试默认进入日常回归，不能按运行失败结果自动归类。已知缺陷没有skip、expectedFailure或“失败当通过”。问题修复后需单独审查对应断言/清单，不在本阶段修改算法。

## 核心文件

| 文件 | 能力 |
| --- | --- |
| `test_signal_causality.py` | 指标数值独立参考、逐前缀因果/重绘、波形与强背离可达性、9个已知缺陷及回放证据生成；原reachability模块已并入 |
| `test_bar_cache_merge.py` | 完整历史合并、数值/身份/时间校验、同bar修订、现行取数器隔离接入 |
| `test_market_data_state.py` | 周期完成状态、新鲜度、时段/日历、断线/恢复、质量及政策接入 |
| `test_signal_policy.py` | candidate/confirmed/invalid/retrospective、不可变确认、追加日志与主扫描正反契约 |
| `test_candidate_quality_gate.py` | 连续观察证据、候选门控、去重/撤销与真实扫描方法 |
| `test_runtime_readiness.py` | 原股票池加载/扫描、六类原输出、跨股票/周期/池隔离、Flask只读兼容 |
| `test_test_entrypoints.py` | 两套测试划分完整、不重叠，9项缺陷不跳过/掩盖 |

相近测试多数分别验证纯函数、适配器或真实调用路径，不因名称相似而删除。测试间的固定合成样本/离线API哨兵继续复用，没有将它们复制进生产代码。

## 手动工具（非回归测试）

仍保留根目录`test_email_send.py`、`test_tg_send.py`、`test_wecom_send.py`：会读取真实配置并发送外部消息，仅用户明确授权时手动使用，本次未运行。

仍保留`test_divergence.py`、`check_s.py`、`debug_sell_s.py`：含指定历史个股/时点的诊断线索，只有打印、没有自动断言，有的导入即读取真实CSV。不能因已有合成回归就宣称覆盖了这些唯一历史案例。`check_daily_signals.py`保留为手工CSV分析工具，不是收益回测。

不要从根目录执行无范围的unittest/pytest发现来运行上述工具。过时且无法导入的`test_issue.py`、`check_api.py`已移出，替代能力及恢复目录见[整理报告](../docs/测试精简整理报告.md)。

## 大型证据重建

原P0-02的输入样本、summary、差异表、时间表及原缺陷日志均保留；仅移出了两份大型逐前缀快照gzip。要在临时目录重建，不要覆盖历史证据目录：

```bash
python -B tests/test_signal_causality.py --export /private/tmp/quant-replay-evidence
```

该命令重新生成全部回放证据，不等于缺陷测试通过。不同pandas/NumPy版本的CSV文本/元数据或gzip字节可能不同，不承诺逐字节一致。旧阶段报告是历史记录，部分旧输出路径已按整理报告迁移，不再视为当前测试入口。
