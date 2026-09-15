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

## 开发阶段

- **P0**: MVP 闭环 (进行中)
