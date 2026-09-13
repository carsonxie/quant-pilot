# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-13 | **分支**: `main` | **最后 Commit**: 未提交 (T0.7 ~ T0.9 工作在工作区)

---

## 当前状态

**P0 仅剩 T0.10：T0.9 自研回测循环完成，六项 Gate 全部通过（6/6），226/226 测试通过。下一步 T0.10 Streamlit 信号面板。**

---

## 最近 Session 完成

### T0.9 — 自研轻量回测循环 v0.1 ⭐ 本次

- **新建** [src/backtest/custom_loop.py](src/backtest/custom_loop.py) — 日线事件循环引擎：
  1. 撮合时序: T 日收盘信号 → **T+1 开盘价成交**（禁收盘价成交）；滑点均值 shift(1) 防未来
  2. 摩擦成本: 单边佣金 0.075%（round-trip 0.15%，用户拍板）+ 每笔滑点 `max(0.15%, 金额/20日均额×0.5%)`（H2.2 模型）
  3. T+0/T+1 元数据路由: 买入按 settlement_type 解锁（T+0 当日 / T+1 次日），锁定期卖单顺延至解锁日；成交记录携 settlement_date
  4. 动态仓位上限（常量/逐日映射）、整手 100 份、卖出先于买入、现金约束、每日净值记录
- **改** [config/settings.yaml](config/settings.yaml) — trading_cost 段重构为无歧义字段（旧字段 bps/注释/宪法三方不一致，已由用户确认口径）+ 新增 backtest.initial_cash
- **新建** [tests/test_custom_loop.py](tests/test_custom_loop.py) — 20 tests（撮合时序/成本精算/交收差一天/锁定顺延/动态仓位/现金约束/真实数据验收）
- **验收**: `uv run backtest` — 510300 买入持有年化 5.75% vs 纯价格基准 5.80%，**偏差 0.044% < 1%**；510300(T+1) 与 513100(T+0) 同日买入，交收日相差 **1 个交易日** ✓

### T0.8 — 消息推送模块 (PushPlus) (上次)

- [src/orchestration/notifier.py](src/orchestration/notifier.py) — 四要素消息、失败重试 3 次、token 未配置安全跳过；`uv run daily` 集成（失败不影响退出码）
- 2026-09-11 真实推送 PushPlus code=200，手机收到 ✓

### T0.7 — 每日盘后主流程编排 (更早)

- [src/orchestration/daily_pipeline.py](src/orchestration/daily_pipeline.py) — 9 步串联五层漏斗，信号幂等落库
- [tests/test_daily_pipeline.py](tests/test_daily_pipeline.py) — 6 tests

### 文档更新

- [docs/PROJECT_MEMORY.md](docs/PROJECT_MEMORY.md) — EV-013（T0.9 设计 + 成本口径），Gate 状态 6/6
- [docs/task.md](docs/task.md) — T0.9 完成，Gate「回测正确性」「T+0/T+1」✅，P0 仅余 T0.10

---

## 进行中

**无进行中 Task。** T0.10 尚未开始，依赖 T0.7/T0.8 已就绪。

---

## 关键决策与原因

| 决策 | 原因 |
|:---|:---|
| **成本口径: round-trip 0.15%（单边 0.075%）** | 用户拍板；原 settings 字段(0.15)+注释(万分之1.5)+宪法(≥0.15%)三方不一致，EV-013 存档 |
| **T+0/T+1 语义: 锁定+顺延** | T+1 份额次日解锁；锁定卖单顺延至解锁日开盘执行；验收以交收日相差 1 交易日为准（用户确认） |
| **验收基准: ETF 自身价格序列** | 用户确认；纯验证引擎记账正确性，跟踪误差不由引擎承担 |
| **滑点均值 shift(1)** | 20 日均额只用 T-1 及之前数据（无未来函数） |
| **推送四要素 + 操作/持有分节** | 可追溯性验收要求；HOLD 折叠避免长列表 |
| **token 走环境变量，不写 settings.yaml** | settings.yaml 被 git 跟踪（EV-012） |
| **前日得分截断重算，不建状态表** | 无状态 = 天然幂等（EV-009） |
| **signal_id 确定性生成** | ON CONFLICT 幂等（EV-009） |

---

## 踩过的坑 / 不要重复的事

| 坑 | 症状 | 解决方案 |
|:---|:---|:---|
| **settings 成本参数三方口径不一致** | bps 单位/注释/宪法互相矛盾 | 用户拍板后重构为无歧义字段 (EV-013)；参数命名必须自洽含单位 |
| **token 不能写 settings.yaml** | settings.yaml 被 git 跟踪，token 会进 git diff | `PUSHPLUS_TOKEN` 环境变量注入 |
| **GBK 日志中文 grep 不到** | stderr logging 乱码字节，UTF-8 关键词匹配失败 | 检索日志用 ASCII 关键词 |
| **Windows GBK 控制台 emoji 崩溃** | `print(summary)` 抛 UnicodeEncodeError | CLI 入口 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` |
| **ruff 基线非零** | 存量 UP045 告警 (旧文件) | 新代码用 `X \| None` 风格保证零新增；不强制全仓修复 |

---

## 环境状态

| 项目 | 值 |
|:---|:---|
| **Python** | 3.14.3 (Windows x64) |
| **包管理** | `uv` (hatchling build backend, editable install) |
| **虚拟环境** | `.venv/` (项目根目录) |
| **数据库** | `data/quant_pilot.duckdb` — 19 ETFs, 34,073 rows OHLCV; trade_signals 11 条（最新 2026-09-11） |
| **数据源** | Sina (主力, 不复权) / eastmoney (备用, 后复权 hfq) |
| **测试** | 226/226 全部通过 |

### 验证命令

```powershell
uv run pytest tests/ -v      # 全量测试 (226)
uv run daily                 # 主流程 + 推送 (token 未配置时安全跳过)
uv run backtest              # 回测: 510300 买入持有 + T+0/T+1 交收对比 (验收 <1% / 差一天)
```

### 项目内导入规则

**所有导入使用顶层模块名**（EV-003）:

```python
from backtest.custom_loop import run_custom_backtest
from orchestration.daily_pipeline import run_daily_pipeline
```

---

## 下一步 (优先级顺序)

### 1. T0.10 — Streamlit 单页面信号面板 ⭐ 唯一剩余

- **文件**: `dashboard/app.py`（新建）
- **依赖**: T0.7, T0.8（已就绪）
- **内容**: 展示今日信号、触发原因、当前持仓表格、近期净值曲线图；从 DuckDB 读取最新信号和持仓数据
- **验收**: 本地 `streamlit run` 后可看到信号面板；信号与 DuckDB 中记录一致
- **注意**: `streamlit`/`plotly` 在 pyproject 的 `p1` optional-deps 组，需要先安装（`uv sync --extra p1` 或加入依赖）；`dashboard/` 尚无文件

### P0 完成后

- P0 Gate 已 **6/6 全通过**；T0.10 完成后 P0 Phase 正式收尾 → 按 AGENTS §1.3 走 Phase 完成流程，然后进入 P1

---

*此文件由 Session 收尾流程自动生成 | 请在下次会话开始时首先阅读此文件*
