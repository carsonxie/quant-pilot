# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-15 | **分支**: `main` | **上一条提交**: `cc123c8 m2 and m3 done`（M4/M5 全部改动未提交）
> **当前批次**: Phase 0.5 — **M5 ✅ 完成**（B0/B1 基准 + 实验记录 + 同口径；411/411 测试）；**M6 待启动**（基础绩效 / 实验补全 / 面板扩展）

---

## 新会话开局必读（按序）

1. `AGENTS.md` — 仓库最高准则（v3.0，与 spec v3.0 对齐）
2. **本文件 HANDOFF.md** — 当前批次、证据、未关闭项与下一步
3. `docs/phase0.5-audit.md` — C01–C23 逐项审计 + §8.1 剩余项（随批次更新）
4. `docs/task-phase0.5.md` — A0 补项 + M1–M7 原子任务与验收（P05-D 对照表已更新）
5. `docs/spec.md`（v3.0）— 业务定义（§8 阶段验收 / §9 审计清单）
6. `docs/plan.md`（v1.0）— M0–M7 实施批次与迁移方案（§5 迁移保护 / §6 关键场景）
7. `docs/PROJECT_MEMORY.md` — EV-001~EV-024、I-01~I-09、§七常用命令

### ⚠️ 关键认知（务必先消化）

- **M1 ✅ / M2 ✅ / M3 ✅ / M4 ✅ / M5 ✅**。M5 = B0/B1 基准模拟 + 实验记录 + `uv run baseline` CLI。
- **M4/M5 全部改动未提交**（新文件：`rebalance_planner.py`、`strategy_states.py`、`rebalance_run.py`、`baseline.py`、`baseline_runner.py` + 3 个测试文件；修改 duckdb_manager / daily_pipeline / proposal_ledger / risk_manager / settings / pyproject / README / 测试 / 文档）。**不要丢弃**。
- **M6 下一步**：基础绩效（期末权益 − 期初权益 − 净外部入金 = 投资盈亏；持仓/分红/费用贡献与差额单列）、收益现金流口径（入金不增加收益）、run/experiment 补全（失败试验保留）、面板扩展（只读连接、实际/目标、现金、待处理建议、反馈入口、净值与 B0/B1/A 对照）。
- **基准为研究示例**：`settings.baselines`（股 70%/黄金 10%/现金 20%，来源 docs/讨论2.md）显式标注非真实 SAA；真实比例由用户后续版本化。`rebalancing.account` 仍为 null（daily 不生成 A 建议）。
- **模拟与真实隔离**：base 账户 `mode=backtest`；真实账户由用户 `account-import` 录入（`mode=real`）。基准 equity 不加到真实权益。
- 真实库 schema **v5**（M4 已迁移）；`instruments`/`accounts`/`trade_proposals` 真实库仍为 0 行（随用户录入/真实 daily 运行写入，属预期）。
- 已知数据问题 I-09：历史含未调整拆分跳变；**基准模拟在拆分年（如 2022）可能有非策略性再平衡**，报告须标注；每日增量遇拆分日会阻断且不自动放行，**勿用全量 fetch-data 绕过**。
- 已知限制 I-03：跨境 ETF 新增买入被 NAV unknown 门槛阻止；解锁=接入 NAV 数据（`nav_daily` 已就绪）。
- 不以文档勾选/提交信息作验收结论；测试用**独立期望值**（spec §9.1）。

---

## 仓库实际状态（2026-09-15 复测）

| 项目 | 值 |
|:---|:---|
| 测试 | **411/411 通过**（`uv run pytest tests/ -q`，~26s；M4 新增 48 + M5 新增 16） |
| 数据库 | 19 ETFs / 34,073 rows / 2019-01-02→2026-09-11；`trade_signals` 11 条（v0.1 未改写）；**schema v5**；账户/建议/状态表空（待用户录入或模拟落库） |
| 备份 | `data/backups/`：pre_v2/v3/v4/v5 快照；演练副本 `rehearsal_*.duckdb`（含 `rehearsal_m5`、`rehearsal_m5_results/`） |
| 基准结果 | `data/backtests/<experiment_id>.parquet|json`（已被 .gitignore 排除）；`experiments` 表记录 |
| 未提交改动 | M4 + M5 全部（见上） |
| Python | 3.14.3；vectorbt 不兼容 = I-02（P1 才需解决） |
| 常用命令 | `docs/PROJECT_MEMORY.md` §七（含 `uv run baseline`） |

---

## M4 完成记录（2026-09-15）

- **调仓规划器** `src/strategies/rebalance_planner.py`（纯函数）：账户估值（缺估值价→暂停）、actual vs target band（5pp 示例）、整手/佣金/滑点/可卖/现金约束、卖出依赖条件建议、四级优先级（风险 90 > 卖 50 > 买 30 > 依赖 10）、有效期、现金预留。
- **状态化 ATR（C06 关闭）** `src/strategies/strategy_states.py` + `calc_atr_series`：持有期高水位、棘轮止损、pending_exit、冷却、重新建仓重置；schema v5 `strategy_states`。
- **daily Step 10** `src/orchestration/rebalance_run.py`：账户存在且配置时生成建议；同日重跑去重、新决策日 replaced；失败不阻断。
- 证据：`tests/test_rebalance_planner.py`（30）+ `tests/test_strategy_states.py`（14）+ `TestRebalanceIntegration`（4）；真实数据副本干跑（止损优先/band 调回/同日去重）。

## M5 完成记录（2026-09-15）

- **B0/B1 模拟器** `src/backtest/baseline.py`：B0 买入持有（初始配置一次建仓）；B1 每周最后交易日检查、模拟实际权重漂移越界整手调回；执行基础与实盘一致（T+1 开盘、卖出先买、批次可卖、佣金最低收费、滑点含价、现金不足缩减）；B0 新增资金政策 `hold_cash` 显式。
- **Runner + CLI** `src/backtest/baseline_runner.py`（`uv run baseline --strategy B0|B1`）：实验记录（代码/数据/池/配置/成本情景/结果位置/状态/结论）+ NAV parquet + 摘要 json；实验 ID 含成本情景名。
- **配置** `settings.baselines`：研究示例（股 70%/黄金 10%/现金 20%，**显式标注非真实 SAA**）；`upsert_experiment`/`get_experiment` 新增。
- **真实数据干跑**（副本，2019-01-02→2026-09-11，无外部现金流）：B0 **+104.72% / 回撤 −31.04% / 7 笔 / 费用 58.75 元**；B1 **+102.16% / 回撤 −29.84% / 14 笔 / 费用 112.36 元**。入金测试（同额同日 20000）两账户独立。
- 证据：`tests/test_baseline.py` 16 例（手工余额/漂移手算/检查日/T+1/滑点/同口径/确定性/实验记录/成本双情景/入金政策/同执行基础）。

### 不要重复的坑

| 坑 | 说明 |
|:---|:---|
| 旧测试"实现自证" | 修旧公式时测试改为独立期望值，不复制实现输出（§9.1） |
| 不可无痕覆盖 | 旧信号/旧建议保留版本（v0.1 信号、replaced 建议）；基准结果按口径标注 |
| DB 备份 | 不复制正在写入的库当可靠备份；迁移前停止写入/一致方式（plan §5） |
| 推送 token | 只走环境变量 `PUSHPLUS_TOKEN`（EV-012） |
| Windows GBK 控制台 | CLI 入口 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` |
| ruff 基线 | 旧文件有存量 UP045；新代码用 `X \| None` 零新增 |
| 宏观重算性能 | 面板 step=5 采样（EV-014） |
| 迁移演练 | schema 变更先在隔离副本演练（plan §5） |
| 拆分/权益事件阻断 | 每日增量遇未调整拆分日阻断且不自动放行；**勿用全量 fetch-data 绕过**（I-09）；基准模拟在拆分年有非策略性调仓，须标注 |
| 不提前开发 | 不启动 Phase 1/1.5（因子平台/ML/归因） |
| 测试值手工重算 | 改测试期望时先手算（整手/最低收费/顺序分配会改变结果），不要凭直觉 |
| 文件编辑 | Bash heredoc/sed 批量改文件曾用于救急；优先用 Edit 工具避免结构破坏 |

---

## 环境与验证命令

```powershell
uv run pytest tests/ -q                      # 全量测试 (411)
uv run daily                                  # 主流程（rebalancing.account=null 时不生成 A 建议）
uv run backtest                               # 回测：偏差 <1% + 交收差一天
uv run baseline --strategy B0 --start 2019-01-02 --end 2026-09-11   # B0 基准
uv run baseline --strategy B1                                        # B1 基准（默认全区间）
uv run account-import --account REAL-1 --name "项目账户" --mode real --csv events.csv
uv run streamlit run dashboard/app.py         # 信号面板
```

项目内导入规则（EV-003，flat layout）：

```python
from backtest.baseline import BaselineConfig, BaselineSimulator
from backtest.baseline_runner import run_baseline
from data_pipeline.accounting import AccountService, AccountEvent
from orchestration.rebalance_run import run_rebalance
from strategies.rebalance_planner import plan_rebalance, value_account
from strategies.strategy_states import load_position_states, update_position_states
```

---

## 下一批（M6）要点与前置

- **M6-01 run/experiment 记录**：补齐 模式/时点/失败状态（`experiments` 现字段已含主要项；必要时 schema v6 增列, 先在副本演练）；失败试验保留（P05-D6）。
- **M6-02 基础绩效**：期末权益 − 期初权益 − 净外部入金 = 投资盈亏；持仓/分红/费用贡献与差额单列（P05-D7）。基准 NAV 序列在 `data/backtests/`；真实账户用事件回放核对。
- **M6-03 收益口径**：现金流口径；只有日线时现金流时点约定与近似标注；参考价滑点不重复扣现金。
- **M6-04 面板扩展**（C21 只读连接）：真实/模拟标签、账户更新时间、实际/目标、现金、待处理建议、反馈入口、数据问题、净值与 B0/B1/A 对照（P05-D8）。
- **M6-05 测试**：零收益 + 入金不改收益率；盈亏闭合；重复导入/重跑不改账。
- 前置：M5（已完成）。

## 收尾要求

- 每批：更新 `docs/phase0.5-audit.md` 与 `docs/task-phase0.5.md`；停靠汇报（完成情况/测试结果/遗留）。
- 阶段结束时：`docs/PROJECT_MEMORY.md`、`README.md`（M7）、重写本文件；对照 **P05-D1–D8** 验收。
- **Phase 0.5 完成 ≠ 允许实盘，也不自动进入 Phase 1**。
