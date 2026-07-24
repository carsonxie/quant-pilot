# HANDOFF.md — Session 交接文档

> **生成时间**: 2025-07-24 | **分支**: `main` | **最后 Commit**: `bc9dd2d`

---

## 当前状态

**P0 MVP 五层决策漏斗全部模块就绪，6/10 交付物完成，待进入 T0.7 主流程编排。**

---

## 本次 Session 完成

### T0.1 数据层补丁 — 数据源从 eastmoney 迁移至 Sina

- **问题**: `uv run fetch-data` 报 `ModuleNotFoundError: No module named 'src'` — `pyproject.toml` 的 CLI entry point 用了 `src.` 前缀，hatchling flat layout 不支持
- **修复**: [pyproject.toml](pyproject.toml) — `src.data_pipeline.xxx:main` → `data_pipeline.xxx:main`
- **eastmoney 持续封禁**: 0.5-3s 休眠后仍在前 3 只 ETF 被限流，无法完成全量导入
- **迁移至 Sina**: [src/data_pipeline/fetchers/akshare_fetcher.py](src/data_pipeline/fetchers/akshare_fetcher.py) 重构为双源架构
  - 主力源: `ak.fund_etf_hist_sina` — 19 ETFs 全量 33,313 rows，4.3 秒完成，零封禁
  - 备用源: `ak.fund_etf_hist_em` — 后复权 (hfq)，保留反封锁机制
  - Symbol 映射: 5xxxxx→sh, 1xxxxx→sz
- **复权修正**: eastmoney 路径 `qfq`→`hfq`（后复权才是量化标准）；Sina 为不复权（ETF 分红影响 <1-2%/年，P0 可接受）
- **配置变更**: [config/settings.yaml](config/settings.yaml) — 新增 `data_fetch.source: "sina"`；ETF 条目改为 inline YAML 格式

### T0.5 — 波动率倒数加权组合优化

- [src/strategies/portfolio_optimizer.py](src/strategies/portfolio_optimizer.py) + [tests/test_portfolio_optimizer.py](tests/test_portfolio_optimizer.py) (43 tests)
- 核心: `w_i ∝ 1/σ_i²`，归一化到仓位上限，单标的 ≤30% 迭代裁剪
- 防御: NaN 检测、数据不足等权回退、最多 5 只截断

### T0.6 — ATR 移动止损 + 宏观骤降强制减仓

- [src/strategies/risk_manager.py](src/strategies/risk_manager.py) + [tests/test_risk_manager.py](tests/test_risk_manager.py) (41 tests)
- ATR: Wilder 平滑 `ATR_t = (ATR_{t-1}×13 + TR_t)/14`
- 止损: `Stop = Trailing_High - N×ATR`，棘轮约束 `max(current, prev)` 只上移不下移
- 骤降: MacroScore 单日下降 > 30 分 → 强制仓位降至 30%

### 文档更新

- [docs/PROJECT_MEMORY.md](docs/PROJECT_MEMORY.md) — EV-007 (Sina 迁移), EV-008 (复权修正), 产出索引更新
- [docs/task.md](docs/task.md) — T0.5, T0.6 标记完成
- [config/settings.yaml](config/settings.yaml) — ETF 条目 inline 格式化 + 数据源配置

---

## 进行中

**无进行中 Task。** T0.7 尚未开始，所有前置依赖已就绪。

---

## 关键决策与原因

| 决策 | 原因 |
|:---|:---|
| **Sina 代替 eastmoney 作为主力数据源** | eastmoney 限流太激进，Sina 零封禁、速度快、历史全。代价是不复权数据，ETF 影响小 |
| **后复权 (hfq) 替代前复权 (qfq)** | 量化标准：后复权保留最早真实交易价，序列始终为正；均线等绝对价格指标更可靠 |
| **Wilder 平滑 ATR 而非 SMA** | SMA 的 ATR 会因旧数据退出而跳跃；Wilder 平滑让止损线稳定变化，减少噪音止损 |
| **止损棘轮只上移不下移** | 核心约束：价格创新高后回落，止损线不下调——"你曾经值这么多，我不接受更低的出场价" |
| **迭代裁剪 vs 简单截断** | 单标的裁剪释放的权重重新分配可能触发二次裁剪，必须迭代直到收敛 |

---

## 踩过的坑 / 不要重复的事

| 坑 | 症状 | 解决方案 |
|:---|:---|:---|
| **eastmoney IP 限流** | `uv run fetch-data` 第 3-4 只 ETF 报 RemoteDisconnected | 切 Sina 数据源（`fund_etf_hist_sina`），需 `sh`/`sz` 前缀 |
| **`ModuleNotFoundError: No module named 'src'`** | CLI entry point 用了 `src.` 前缀 | hatchling flat layout 安装后无 `src` 包，entry point 用 `data_pipeline.xxx` |
| **前复权 ≠ 量化标准** | CLAUDE.md 写错，历史价格可能变负 | 后复权 (hfq) 才是正确的；CLAUDE.md 待修订 |
| **迭代裁剪链式反应** | 单标的裁剪→释放权重→接收方也超限 | 必须循环重分配直到无新裁剪（max 10 轮） |
| **测试随机数据触发止损** | `make_ohlcv` 随机种子导致 ETF_C 偶然跌破止损线 | 确定性数据（`make_trending_ohlcv`）测正常路径，暴跌序列测触发路径 |
| **I-02: vectorbt 不兼容 Python 3.14** | llvmlite 不支持 Python >3.12 | vectorbt 已移到 optional-deps p1 分组；P1 时 `uv python pin 3.12` |

---

## 环境状态

| 项目 | 值 |
|:---|:---|
| **Python** | 3.14.3 (Windows x64) |
| **包管理** | `uv` (hatchling build backend, flat layout) |
| **虚拟环境** | `.venv/` (项目根目录) |
| **数据库** | `data/quant_pilot.duckdb` — 19 ETFs, 33,313 rows |
| **数据源** | Sina (主力, 不复权) / eastmoney (备用, 后复权 hfq) |
| **测试** | 178/178 全部通过 |

### 验证命令

```powershell
# 全量测试
uv run pytest tests/ -v

# 数据重新拉取（慎用 — 会覆盖 DuckDB）
uv run fetch-data

# 快速冒烟：拉取 + 校验一条龙
uv run python -c "
from data_pipeline.fetchers.akshare_fetcher import AkshareFetcher
f = AkshareFetcher(source='sina')
df = f.fetch(['510300'], '20260101', '20260719')
print(f'{len(df)} rows, columns={df.columns}')
"
```

### 项目内导入规则

**所有导入使用顶层模块名**（EV-003）:
```python
# ✅ 正确
from data_pipeline.fetchers.akshare_fetcher import AkshareFetcher
from strategies.macro_timing import MacroTiming

# ❌ 错误
from src.data_pipeline.fetchers.akshare_fetcher import AkshareFetcher
```

---

## 下一步 (优先级顺序)

### 1. T0.7 — 每日盘后主流程编排 ⭐ 最优先
- **文件**: `src/orchestration/daily_pipeline.py`（新建）
- **内容**: 串联数据拉取 → 质检 → 宏观择时 → 标的清洗 → 组合优化 → 风控 → 信号生成
- **依赖**: T0.2-T0.6 全部已就绪
- **验收**: 运行 `uv run daily` 全流程无报错，日志完整
- **参考**: [docs/task.md](docs/task.md) P0 T0.7; [docs/spec.md](docs/spec.md) §3 编排层

### 2. T0.8 — 消息推送模块
- **文件**: `src/orchestration/notifier.py`（新建）
- **依赖**: T0.7 完成后
- **内容**: PushPlus/飞书推送，含触发原因归因

### 3. T0.9 — 自研轻量回测循环
- **文件**: `src/backtest/custom_loop.py`（新建）
- **依赖**: T0.5 + T0.6（已就绪）
- **内容**: T+0/T+1 交收区分、动态仓位、摩擦成本

---

*此文件由 Session 收尾流程自动生成 | 请在下次会话开始时首先阅读此文件*
