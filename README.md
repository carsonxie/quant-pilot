# Quant-Pilot

量化辅助决策与投研系统 — 基于 A 股 ETF 的五层决策漏斗。

## 快速开始

```powershell
# 安装依赖
uv sync
# 激活虚拟环境
.venv\Scripts\activate
# 初始化数据库并拉取数据
uv run fetch-data
```

## 账户事件录入（Phase 0.5）

真实账户的成交/出入金**由用户录入**（系统不连接券商、不自动下单）。
标准 CSV 模板：`docs/account_events_template.csv`；重复导入自动去重；记录错误用"更正"事件。

```powershell
uv run account-import --account REAL-1 --name "项目账户" --mode real --csv events.csv
```

- 事件类型：initial / deposit / withdrawal / trade / fee / dividend_accrual / dividend_payment / share_change / correction
- 同一笔成交只走一个通道：直接记事件（本命令）**或**执行反馈联动入账（M4 起），二者不可同时使用
- 写入并发约束：daily / account-import / 迁移同一时间只运行一个（DuckDB 单写进程）
- 未录入真实账户时系统保持 research 模式运行（面板与推送均标注模拟）

## 基准模拟 B0/B1（Phase 0.5 M5）

固定研究配置（`config/settings.yaml → baselines`，**研究示例**：股 70% / 黄金 10% / 现金 20%，
来源 docs/讨论2.md，非真实 SAA）在**独立模拟账户**上运行：

```powershell
uv run baseline --strategy B0 --start 2019-01-02 --end 2026-09-11    # 买入持有
uv run baseline --strategy B1                                        # 固定目标 + 阈值再平衡 (默认全区间)
uv run baseline --strategy B0 --write-account                        # 同时把模拟账户写入数据库
```

- B0/B1 共用同一执行基础（T+1 开盘成交、整手、佣金/最低收费、批次可卖、现金约束）
- B1 检查日 = 每周最后交易日；`band_points`（5pp 示例）由实际权重漂移触发（目标不变也检查）
- 结果：`data/backtests/<experiment_id>.parquet`（NAV 序列）+ `.json`（摘要）；实验记录写 `experiments` 表
- 成本情景命名来自 `trading_cost.scenario`（当前 `legacy_stress` = 旧压力情景，非校准）
- 已知限制：Sina 不复权价格不含分红/权益事件（含未调整拆分跳变，I-09）；期间收益按同口径比较

## 开发阶段

- **P0**: MVP 闭环 (进行中)
