# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-13 | **分支**: `main` | **最后 Commit**: 未提交 (T0.7 ~ T0.10 工作在工作区)

---

## 当前状态

**🎉 P0 Phase 完成：10/10 交付物全部交付，Gate 6/6 全通过，242/242 测试通过。下一步 = P1 启动（起点 T1.1 因子注册表 + 因子计算模块）。**

---

## 最近 Session 完成

### T0.10 — Streamlit 信号面板 (P0 收官) ⭐ 本次

- **新建** [dashboard/data_loader.py](dashboard/data_loader.py) — 纯数据层（不 import streamlit, 可单测）：
  最新信号 / 建议目标持仓视图 / 归一化价格曲线（首日=100）/ 宏观得分历史（逐日截断重算, 展示用 step=5 采样, 始终含最新交易日）
- **新建** [dashboard/app.py](dashboard/app.py) — 单页面: 宏观状态卡 → 今日信号表（含触发原因）→ 建议目标持仓（空仓友好提示）→ 持仓标的归一化走势（空仓回退 510300）→ 宏观得分趋势（近 120 交易日）
- **新建** [tests/test_dashboard.py](tests/test_dashboard.py) — 16 tests（含 streamlit AppTest 无头冒烟 + 真实库"信号与 DB 记录一致"验收）
- **改** [pyproject.toml](pyproject.toml) — streamlit 1.59.2 从 p1 optional 组移入主依赖（Python 3.14 兼容已验证）；dashboard 加入 hatchling packages
- **验证**: headless 服务 `/_stcore/health` = ok, 首页 HTTP 200; 全量 242/242 通过
- **运行**: `uv run streamlit run dashboard/app.py`

### T0.9 — 自研轻量回测循环 (上次)

- [src/backtest/custom_loop.py](src/backtest/custom_loop.py) — T+1 开盘成交 / 摩擦成本（单边佣金 0.075% + 滑点 max(0.15%, 金额/20日均额×0.5%)）/ T+0/T+1 交收路由 / 动态仓位 / 每日净值
- **验收**: 510300 买入持有年化偏差 0.044% < 1%; 交收日相差 1 个交易日 ✓

### T0.7 / T0.8 — 主流程编排 + PushPlus 推送 (更早)

- [src/orchestration/daily_pipeline.py](src/orchestration/daily_pipeline.py) — 9 步五层漏斗, 信号幂等落库
- [src/orchestration/notifier.py](src/orchestration/notifier.py) — 四要素推送（2026-09-11 真实推送成功）

### 文档更新

- [docs/PROJECT_MEMORY.md](docs/PROJECT_MEMORY.md) — EV-014（面板设计 + P0 收官），Phase 状态 ✅, Gate 6/6
- [docs/task.md](docs/task.md) — T0.10 完成，Phase 0 标记 ✅ 完成

---

## 进行中

**无进行中 Task。P0 全部完成，等待 P1 启动决策。**

---

## 关键决策与原因

| 决策 | 原因 |
|:---|:---|
| **面板"持仓/曲线"语义（用户确认）** | 无实盘数据 → 展示"建议目标持仓"（信号 target_weight>0）+ 持仓标的归一化走势（空仓回退基准 510300） |
| **数据层与 UI 解耦** | `data_loader.py` 纯函数可单测（16 tests, 含 AppTest 冒烟）; app.py 只渲染 |
| **宏观趋势 step=5 采样** | 逐日重算 120 天 ~13s 过慢; 采样 ~2.5s; 测试保留 step=1 与直接计算一致性 |
| **streamlit 移入主依赖** | P0-D10 交付必需; Python 3.14 兼容已验证; 图表用原生组件不引入 plotly |
| **成本口径: round-trip 0.15%** | 用户拍板（EV-013） |
| **验收基准: ETF 自身价格** | 用户确认（EV-013） |
| **token 走环境变量** | settings.yaml 被 git 跟踪（EV-012） |

---

## 踩过的坑 / 不要重复的事

| 坑 | 症状 | 解决方案 |
|:---|:---|:---|
| **宏观逐日重算过慢** | 120 天逐日截断重算 ~13s | step 采样 + 保证含最新交易日; 缓存 TTL 300s |
| **settings 成本参数三方口径不一致** | bps/注释/宪法矛盾 | 用户拍板后无歧义重构 (EV-013) |
| **token 不能写 settings.yaml** | 被 git 跟踪 | `PUSHPLUS_TOKEN` 环境变量 |
| **Windows GBK 控制台** | emoji/中文日志问题 | CLI reconfigure utf-8; 日志检索用 ASCII 关键词 |
| **ruff 基线非零** | 存量 UP045 (旧文件) | 新代码 `X \| None` 风格零新增 |

---

## 环境状态

| 项目 | 值 |
|:---|:---|
| **Python** | 3.14.3 (Windows x64) |
| **包管理** | `uv` (hatchling editable install) |
| **数据库** | `data/quant_pilot.duckdb` — 19 ETFs, 34,073 rows; trade_signals 11 条（最新 2026-09-11） |
| **新依赖** | streamlit 1.59.2 (主依赖, 已装) |
| **测试** | 242/242 全部通过 |

### 验证命令

```powershell
uv run pytest tests/ -v                        # 全量测试 (242)
uv run daily                                   # 主流程 + 推送 (token 未配置时安全跳过)
uv run backtest                                # 回测: 偏差 <1% / 交收差一天
uv run streamlit run dashboard/app.py          # 信号面板 (默认 http://localhost:8501)
```

### 项目内导入规则

**所有导入使用顶层模块名**（EV-003）:

```python
from backtest.custom_loop import run_custom_backtest
from dashboard.data_loader import load_latest_signals
from orchestration.daily_pipeline import run_daily_pipeline
```

---

## 下一步 (P1 启动清单)

### 0. ⚠️ 启动前决策 — I-02: Python 3.14 vs vectorbt
- T1.9（vectorbt 对照）与 T1.5（alphalens-reloaded）需要验证在 Python 3.14 的可用性
- 选项: `uv python pin 3.12` 重建环境（一次到位） / 保持 3.14 先做 T1.1-T1.4（不依赖这两个库） / Docker 隔离
- **需要用户拍板**；未解决前 T1.9/T1.5 标记 `[!]`

### 1. T1.1 — 因子注册表初始化 + 因子计算模块 ⭐ P1 起点
- `config/factor_registry.yaml`（当前空表）填充 ≥ 8 个因子（动量/波动/成交量/估值等）
- `src/factor_engineering/technical.py` / `fundamental.py` / `macro.py`

### 2. 其余按 task.md Phase 1 顺序（T1.2 CI 校验 → T1.3 预处理 → T1.4 截面打分 → ...）

---

*此文件由 Session 收尾流程自动生成 | 请在下次会话开始时首先阅读此文件*
