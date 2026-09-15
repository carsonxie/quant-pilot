# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-14 | **分支**: `main` | **已提交**: `a753319 m 1-1 and 1-2 done`（M1-3 + AGENTS v3.0 + M2 + M3 未提交）
> **当前批次**: Phase 0.5 — **M3 ✅ 完成**（四层池 + 产品契约 + NAV 门槛，346/346 测试）；**M4 待启动**（目标→建议统一）

---

## 新会话开局必读（按序）

1. `AGENTS.md` — 仓库最高准则（**已更新至 v3.0，与 spec v3.0 对齐**；对照表见审计 §4.1）
2. **本文件 HANDOFF.md** — 当前批次、证据、未关闭项与下一步
3. `docs/phase0.5-audit.md` — **C01–C23 逐项审计 + §8.1 剩余项**（工作状态的核心记录，随每批更新）
4. `docs/task-phase0.5.md` — A0 补项 + M1–M7 原子任务与验收（旧 `docs/task.md` 保留为历史参考）
5. `docs/spec.md` (v3.0) — 业务定义（§8 阶段验收 / §9 审计清单）
6. `docs/plan.md` (v1.0) — M0–M7 实施批次与迁移方案（§5 迁移保护 / §6 关键场景）
7. `docs/PROJECT_MEMORY.md` — EV-001~EV-020、I-01~I-09、§七常用命令

### ⚠️ 关键认知（务必先消化）

- **M1 已完成**（A 类纠偏 C01/C04/C05/C06/C08/C09/C10/C11 + C03 部分；Phase 0 §8.1 补项 A0-1…A0-6 全部闭合；AGENTS.md v3.0 已获用户确认）。
- **M2 ✅ 完成**（账户 + 四类记录 + schema v3）。**M3 ✅ 完成**（四层池：产品契约/池版本/可交易许可/NAV 门槛；schema v4）。
- **M4 下一步**：目标→建议统一（账户对比、数量规划、现金/可卖/优先级/有效期、卖出依赖、重新进入与冷却；规划器先隔离于策略与账本）。
- **用户录入通道已可用**：`uv run account-import --account <ID> --name <名称> --mode real --csv <文件>`（模板 `docs/account_events_template.csv`，同一文件重复导入自动去重）；真实成交必须由用户录入（系统不接券商），paper/backtest 账户由系统生成。
- 真实库已迁移 schema v2：信号带 `strategy_version`，旧 11 条标注 **v0.1**（内容逐行不变）；最新交易日 2026-09-11 的信号仍为 v0.1，下一交易日 daily 运行将自然产生 v0.2（不重跑旧信号日）。
- 已知数据问题 I-09：历史含未调整拆分跳变；增量遇拆分日会阻断（隔离设计），**勿用全量 fetch-data 绕过**；修复在 M2/M3。
- 不以文档勾选/提交信息作验收结论；测试修复旧公式时用**独立期望值**（spec §9.1）。

---

## 仓库实际状态（2026-09-14 复测）

| 项目 | 值 |
|:---|:---|
| 测试 | **346/346 通过**（`uv run pytest tests/ -q`，~19s；M3 新增 23 项） |
| 数据库 | 19 ETFs / 34,073 rows / 2019-01-02→2026-09-11；`trade_signals` 11 条（v0.1 未改写）；**schema v4**；`instruments` 19 只 + 池版本 `uv-abc0b4d53f` + `targets`/`runs` 由 daily 写入；`accounts`/`account_events` 空（待用户录入或模拟账户）；`nav_daily` 空（跨境禁买中，I-03） |
| 备份 | `data/backups/`：pre_v2 / pre_v3 / pre_v4 一致快照、信号留档 CSV、演练副本 `rehearsal_*.duckdb` |
| 未提交改动 | M1-3 + AGENTS v3.0 + M2 + M3（accounting.py / proposal_ledger.py / etf_pool.py / product_compare.py 新文件，duckdb_manager / daily_pipeline / universe_filter / settings / pyproject / README / 测试 / docs）|
| 已知数据问题 | 历史含未调整拆分跳变（I-09）；每日增量遇拆分日会阻断（隔离设计）；跨境新增买入被 NAV 门槛阻止（I-03） |
| Python | 3.14.3；vectorbt 不兼容 = I-02（P1 才需解决） |
| 常用命令 | `docs/PROJECT_MEMORY.md` §七（含 `uv run account-import`） |

---

## M1 完成记录（全部 2026-09-14）

- **M1-1 ✅ 公式与成本（A 类核心，2026-09-14 完成）**：C01（1/σ 修正 + inverse_variance 分离 + 独立期望值 2:1/4:1）、C04（退出/清零/止损/骤降/首次建仓绕过防抖）、C08（最低收费接线、`legacy_stress` 命名、手工往返对账）。新增 18 测试 → 260/260；真实回测偏差 0.044% 保持。
- **M1-2 ✅ 数据、时序与证据（2026-09-14 完成）**：C10（产品感知涨跌阈值 10%/20% 板 + 容差分级 + NAV unknown 显式）、C11（"ETF 代理宽度"命名 + `scope=a_share` 分桶、非A独立上限 0.30 研究示例）、C05（批次可卖 `_Lot` + 现金可用/可取）、C06（跳空独立证据 + ATR 近似声明）、C09（不复权/无分红口径标注 + 真实拆分事件清单）。新增 16 测试 → 276/276；真实回测 0.044% 保持；真实数据副本干跑验证。
- **M1-3 ✅ 标注与收尾（2026-09-14 完成）**：推送/面板"研究/模拟、无真实账户、数据截止、策略版本"；`trade_signals.strategy_version` schema v2（旧记录标注 v0.1，真实库迁移内容不变，备份于 `data/backups/`）；副本真实网络重跑（混版共存）；§8.1 清单闭合。**C13 关闭：AGENTS.md 已更新至 v3.0。**

## M2/M3 完成记录（2026-09-14）与 M4 要点

- **M2 ✅ 账户 + 四类记录 + schema v3**：`accounting.py`（9 类事件/回放/更正/去重/批次/现金口径）；`proposal_ledger.py`（建议状态机、反馈幂等、部分成交/未执行/超额、反馈联动入账、目标替代）；daily 写 `targets`（含现金权重与版本）+ `runs`（推送状态持久化）；用户通道 CSV + `account-import`（README 手册）。
- **M3 ✅ 四层池 + 产品契约 + NAV 门槛（schema v4）**：
  - `instruments`（19 只：交易所/市场类型/资产类别/指数/上市日近似/结算/涨跌限制/溢价必需）+ `universe_versions`/`universe_members`（内容哈希 `uv-…`、生效区间、出池标注不删历史）+ `nav_daily`。
  - `etf_pool.py`：分项检查 pass/fail/unknown/not_applicable；can_buy/can_sell/can_hold；**禁买≠清仓**；停牌禁买卖保留持仓；`member_buyable_at`（历史：上市日前不可买/声明日前条件性研究）。
  - **行为变更**：缺 NAV 且溢价必需（默认跨境）→ 禁新买（实测 14/19 可买）；停牌持仓信号 HOLD 保留原目标。
  - `product_compare.py`：相关矩阵（共同交易日/窗口/样本/跳过原因）、同指数分组、对照表（不按阈值删池）。
- **M4 下一步**：目标→建议统一 — 共用规划逻辑（目标 vs 账户实际权重 → 数量/现金/可卖/费用/有效期/优先级）、风险优先级顺序、绝对百分点 band 再平衡、卖出依赖与重新进入/冷却、现金预留。
- **纪律**：schema 变更先隔离副本演练；真实成交事实不得被重算覆盖；每批停靠汇报。

### 不要重复的坑

| 坑 | 说明 |
|:---|:---|
| 旧测试"实现自证" | 修旧公式时测试改为独立期望值，不复制实现输出（§9.1） |
| 不可无痕覆盖 | 旧 3 个信号日（1/σ² 口径）保留；新版本只对新日期生成；报告标注口径（§9.3） |
| DB 备份 | 不复制正在写入的库当可靠备份；迁移前停止写入/一致方式（plan §5） |
| 推送 token | 只走环境变量 `PUSHPLUS_TOKEN`；token 不写入 settings/文档（EV-012） |
| Windows GBK 控制台 | CLI 入口 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；日志检索用 ASCII 关键词 |
| ruff 基线 | 旧文件有存量 UP045 告警；新代码用 `X \| None` 零新增 |
| 宏观重算性能 | 面板 step=5 采样，勿改回逐日（EV-014） |
| 迁移演练 | 任何 schema 变更先在隔离副本演练（plan §5） |
| 拆分/权益事件阻断 | 每日增量遇未调整拆分日（10 天回溯会持续命中）触发 critical 阻断且不自动放行；须人工核验；**勿用全量 fetch-data 绕过**（会写入跳变，2026-07-19 先例）；修复在 M2/M3（I-09） |
| 不提前开发 | 不启动 Phase 1/1.5（因子平台/ML/归因） |

---

## 环境与验证命令

```powershell
uv run pytest tests/ -q                      # 全量测试 (242)
uv run daily                                  # 主流程（token 未配置时推送安全跳过）
uv run backtest                               # 回测：偏差 <1% + 交收差一天
uv run streamlit run dashboard/app.py         # 信号面板
```

项目内导入规则（EV-003，flat layout）：

```python
from backtest.custom_loop import run_custom_backtest
from dashboard.data_loader import load_latest_signals
from orchestration.daily_pipeline import run_daily_pipeline
```

---

## 收尾要求

- 每批：更新 `docs/phase0.5-audit.md`（状态+证据+迁移影响）与 `docs/task-phase0.5.md`（勾选+证据）；停靠汇报（完成情况/测试结果/遗留）。
- 阶段结束时：更新 `docs/PROJECT_MEMORY.md`（EV 追加、快照、I 系列）、`README.md`（M7）、重写本文件；对照 **P05-D1–D8** 验收。
- **Phase 0.5 完成 ≠ 允许实盘，也不自动进入 Phase 1**。
