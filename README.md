# Quant-Pilot

量化辅助决策与投研系统 — 基于 A 股 ETF 的量化辅助决策与投研系统（信号生成，人工执行；不自动下单）。

> **第一次使用？请先读 [`docs/user_guide.md`](docs/user_guide.md)** —— 从安装、配置、添加 ETF、
> 录入交易到读懂输出的完整手册。

> **状态**: Phase 0.5 已完成（M1–M7）。**仅为研究与模拟用途** —— 未接入真实账户、不构成投资建议；
> Phase 0.5 完成 ≠ 允许实盘，也不自动进入下一阶段。真实账户启用需另行评估（见文末"安全边界"）。

## 快速开始

```powershell
# 安装依赖 (uv)
uv sync
# 激活虚拟环境
.venv\Scripts\activate
# 初始化数据库并拉取数据 (首次: 全量; 之后用 daily 增量)
uv run fetch-data
# 每日盘后流程 (增量取数 → 质检 → 五层漏斗 → 信号落库 → 推送)
uv run daily
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `uv run daily` | 每日盘后主流程（推送需 `PUSHPLUS_TOKEN` 环境变量；未配置时安全跳过） |
| `uv run fetch-data` | 全量/增量取数（历史重建；⚠️ 勿用于绕过拆分日阻断, 见"数据问题"） |
| `uv run backtest` | 回测（510300 买入持有偏差 <1% + T+0/T+1 交收对照） |
| `uv run baseline --strategy B0\|B1` | B0/B1 基准模拟（研究示例配置; `--write-account` 落库模拟账户） |
| `uv run account-import ...` | 账户事件录入（真实成交/出入金唯一通道） |
| `uv run streamlit run dashboard/app.py` | 面板：账户与信号（只读）＋ **单标的诊断**（Phase 1.1；刷新/观察名单经受控写入口） |
| `uv run pytest tests/ -q` | 全量测试（663） |

## 账户事件录入（Phase 0.5）

真实账户的成交/出入金**由用户录入**（系统不连接券商、不自动下单）。
标准 CSV 模板：`docs/account_events_template.csv`；重复导入自动去重；记录错误用"更正"事件。

```powershell
uv run account-import --account REAL-1 --name "项目账户" --mode real --csv events.csv
```

- 事件类型：initial / deposit / withdrawal / trade / fee / dividend_accrual / dividend_payment / share_change / correction
- 同一笔成交只走一个通道：直接记事件（本命令）**或**执行反馈联动入账（M4 起），二者不可同时使用
- 写入并发约束：daily / account-import / 基准模拟 / 迁移同一时间只运行一个（DuckDB 单写进程）
- 未录入真实账户时系统保持 research 模式运行（面板与推送均标注模拟）
- 录入后，把账户 ID 写入 `config/settings.yaml → rebalancing.account`，daily 才会生成可执行建议

## 基准模拟 B0/B1（Phase 0.5 M5）

固定研究配置（`config/settings.yaml → baselines`，**研究示例**：股 70% / 黄金 10% / 现金 20%，
来源 docs/讨论2.md，**不是**用户的真实 SAA）在**独立模拟账户**上运行：

```powershell
uv run baseline --strategy B0 --start 2019-01-02 --end 2026-09-11    # 买入持有
uv run baseline --strategy B1                                        # 固定目标 + 阈值再平衡 (默认全区间)
uv run baseline --strategy B0 --write-account                        # 同时把模拟账户写入数据库
```

- B0/B1 共用同一执行基础（T+1 开盘成交、整手、佣金/最低收费、批次可卖、现金约束）
- B1 检查日 = 每周最后交易日；`band_points`（5pp 示例）由实际权重漂移触发（目标不变也检查）
- 结果：`data/backtests/<experiment_id>.parquet`（NAV 序列）+ `.json`（摘要）；实验记录写 `experiments` 表（含失败留痕）
- 成本情景命名来自 `trading_cost.scenario`（当前 `legacy_stress` = 旧压力情景，非校准）
- 已知限制：Sina 不复权价格不含分红/权益事件（含未调整拆分跳变，I-09）；期间收益按同口径比较

## 绩效口径（Phase 0.5 M6）

- 金额核对：**期末权益 − 调整起点 − 净外部入金 = 投资盈亏**；持仓/现金贡献、分红、费用分项单列，未解释差额显式（应为 0）
- 入金/出金不计收益；分红计收益；费用只以账本 fee 进入现金（参考价滑点不二次扣）
- 只有日线数据时，现金流时点按"日终估值点"近似并随报告标注，不把未知时点当作精确 TWR
- 面板（只读连接）提供：实际 vs 目标、待处理建议与反馈、B0/B1/A 净值对照（各账户独立，不相加）

## 数据问题与操作约束

- **I-09 未调整拆分/权益事件**：历史数据含跳变。已建**权益事件登记表**（`equity_events`, schema v7）与
  **拆分感知质检**：命中已登记事件（日期+比例一致）→ 记为 info 不阻断；未登记跳变仍按 critical 隔离。
  - 查看未登记跳变：`uv run python scripts/register_equity_events.py --check`
  - 登记已核验清单（幂等）：`uv run python scripts/register_equity_events.py --apply`
  - 当前已登记 12 条（11 条份额折算 + 512690 2021-12-31），全部为**推断值待核验**；官方数据接入后升级。
  - ⚠️ 仍**不得用全量 `fetch-data` 绕过**未登记跳变；应人工核验后登记，再让 daily 正常通过。
- **I-03 跨境 ETF 缺 NAV**：溢价检查为必需的产品缺 NAV → 禁新买（unknown 记录），持仓保留；
  解锁需接入 NAV 数据（`nav_daily` 表已就绪）。
- **数据质检**：价格为负 / 超出产品涨跌板（含容差）→ 阻断；20% 板产品用 overrides；真实极端行情不被改写。
- **推送 token**：只走环境变量 `PUSHPLUS_TOKEN`；token 不写入配置与日志。

## 开发阶段与安全边界

- Phase 0 / 0.5 已完成（数据管道、五层漏斗、账户与四类记录、ETF 四层池、目标→建议统一、
  B0/B1 基准、绩效与面板）。
- 后续：Phase 1（少量因子验证）、Phase 1.5（风险/因子归因与策略治理）、Phase 2（可选 ML）、Phase 3（远期）。
- **Gate**：工程 Gate（公式/时序/账本/错误处理）与 P05 验收已完成；**真实账户启用 Gate 未触发** ——
  需账户核对、明确配置、缺陷关闭、前瞻运行证据与用户明确启用，均不随本阶段完成自动满足。
