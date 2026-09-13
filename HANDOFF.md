# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-13 (晚) | **分支**: `main` | **最后 Commit**: `833aff7 t0.10 Phase 0 done`
> **工作区未提交**: `docs/spec.md`（v3.0 重写）+ `docs/plan.md`（新增）

---

## 下阶段任务：Phase 0 收尾 + Phase 0.5（spec v3.0 / plan.md 新定义）

**新会话开局必读（按序）**：

1. `AGENTS.md` — 仓库最高准则（根目录）
2. **本文件 HANDOFF.md** — 仓库实际状态、已知陷阱与审计线索
3. `docs/spec.md` (v3.0) — 新业务定义（重点 §8.1 / §8.2 / §9）
4. `docs/plan.md` (v1.0) — M0–M7 实施批次
5. `docs/PROJECT_MEMORY.md` — 演化记录 EV-001~EV-014、未解决问题 I-01~I-05、§七常用命令
6. `docs/task.md` — 旧任务记录（T0.1~T0.10 已完成，仅作历史参考）

### ⚠️ 关键认知差（务必先消化）

spec v3.0 §0.1 里的进度快照（"T0.8 完成、T0.9/T0.10 未完成"）**已过时**——仓库实际 **T0.1~T0.10 全部完成**（上一会话交付：242/242 测试、真实推送 code=200、回测偏差 0.044%、面板已验收）。

但——**Phase 0 是否"收尾"必须按 spec v3.0 §8.1 的验收证据重新核对**：新口径比旧 task 更严（现金与份额手工核对、涵盖分红与费用、不依赖指数年度误差作唯一检验、买入批次可卖规则、面板模拟状态标注等）。不要把任何文档的勾选或"Phase 0 done"提交当作代码验收结论（spec §9.1 原话）。

### background.md 待办

plan §1 要求把 `docs/background.md` 替换为 v3.0 正文，**当前仓库仍是旧版**（"导师联合规划书"）。启动时先与用户确认 v3.0 版是否就绪；未就绪则记录并询问，**不要自行改写背景文档**。

---

## 仓库实际状态（截至本文件生成）

| 项目 | 值 |
|:---|:---|
| 交付物 | T0.1~T0.10 全部完成（数据/护栏/择时/清洗/组合/风控/主流程/推送/回测/面板） |
| 测试 | **242/242 通过**（`uv run pytest tests/ -v`） |
| 数据库 | `data/quant_pilot.duckdb` — 19 ETFs, 34,073 rows OHLCV; trade_signals 11 条（最新 2026-09-11） |
| Python | 3.14.3；vectorbt 不兼容 = I-02（**本阶段不阻塞**，Phase 1 才需解决） |
| 常用命令 | `docs/PROJECT_MEMORY.md` §七（fetch-data / daily / backtest / streamlit / pytest） |

---

## 已知审计线索（高概率受影响的 C 项）

> 以下为上一会话对 C01–C23 的初步对照，**仅作线索**。正式结论必须由 M0 按 spec §9 流程核对代码后写入 `docs/phase0.5-audit.md`。

| 编号 | 线索 | 位置 |
|:---|:---|:---|
| C01/A | 实现名为 `inverse_volatility` 但实际公式 `w ∝ 1/σ²`（task.md T0.5 原文即 1/σ²） | `strategies/portfolio_optimizer.py` |
| C04/A | 15% 防抖以 `abs(delta) < threshold → HOLD 维持原仓` 实现——**小仓位（如 10%）的清仓/止损剔除可能被挡成 HOLD 而不卖出** | `daily_pipeline._step_generate_signals` |
| C03/A | 全链路"信号=目标"，无真实账户；`trade_signals` 直接充当面板来源——需 mode/模拟标注与真实账本分离 | `duckdb_manager` / `daily_pipeline` / `dashboard` |
| C05/A | T0.9 交收验收仅"日期差一天"；需补买入批次可卖时间与现金可用/可取建模 | `backtest/custom_loop.py` |
| C08/A | 佣金已按 round-trip 0.15%（单边 0.075%，无重复扣 ✓）；滑点 `max(0.15%, …)` 需按新 spec 降级为"明确命名的旧压力情景" | `config/settings.yaml` / `custom_loop.py` |
| C09/A | Sina 不复权 + 无分红/权益事件处理（I-04 已知） | `fetchers` / 权益事件 |
| C10/A | 缺 NAV 静默跳过（I-03/E V-006）；validator 固定涨跌幅阈值 | `universe_filter.py` / `validator.py` |
| C11/A | 19 只 ETF 的 breadth 充当"市场宽度"（分母命名不符）；A股得分统一限制全部资产（含黄金/跨境） | `macro_timing.py` |
| C12/B | 无历史池版本（用当前池回测全部历史） | `etf_metadata` / `universe_filter` |
| C13/B | AGENTS §5.2 的"对数收益/ADF/行业市值中性化"条款与 ETF 场景不符——涉及仓库规则文件，需与用户确认修订 | `AGENTS.md` / `factor_engineering` |
| C20/C21/B | mode 隔离、DuckDB 并发写入协调 | `duckdb_manager` / 调度 |

优先级定义（spec §9.1）：**A=正确性前置（继续验收前修正）/ B=Phase 0.5 闭合 / C=规格先修正按阶段实现**。

---

## 新会话执行要点

1. **M0**：核对仓库、运行证据与未提交改动 → 创建 `docs/phase0.5-audit.md`（C01–C23 逐项：实际证据 / 是否受影响 / 优先级 / 关联批次 / 验证方式 / 历史影响 / 状态）
2. **M0**：生成 `docs/task-phase0.5.md`（Phase 0 补项 + M2–M7 原子任务；保留旧 `task.md` 不覆盖）
3. **M1 起持续实施**：先修正相关 A 类问题并补齐 Phase 0，再完成 Phase 0.5——**不要停在只生成计划**
4. 每批：实现 → 针对性验证 → 更新任务/审计记录 → 简报；全部完成后对照 **P05-D1–D8** 验收
5. 约束：复用现有实现；保留用户改动与真实历史数据；迁移按 **plan §5**（git 状态检查 → 备份 → 隔离副本演练 → 半笔事件保护）；研究配置明确标注；**不自动启用真实资金；不提前开发 Phase 1/1.5**
6. 普通技术选择自行决定并记录；仅关键业务冲突、权限不足或不可逆操作才询问用户

---

## 注意事项 / 不要重复的坑

| 坑 | 说明 |
|:---|:---|
| **旧测试"实现自证"** | 修复旧公式时，测试要改为独立期望值验证，不能把实现输出复制成新断言（§9.1） |
| **不可无痕覆盖** | 已生成信号/净值/成交不得无痕覆盖；修正后生成新版本，旧报告标明口径（§9.3） |
| **DB 备份** | 不能把正在写入的数据库直接复制当可靠备份（plan §5） |
| **推送 token** | 只走环境变量 `PUSHPLUS_TOKEN`；`settings.yaml` 被 git 跟踪，token 不得写入（EV-012） |
| **Windows GBK 控制台** | CLI 入口需 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；日志检索用 ASCII 关键词 |
| **ruff 基线** | 旧文件有存量 UP045 告警；新代码用 `X \| None` 风格保持零新增 |
| **宏观重算性能** | 面板宏观历史逐日重算过慢，已用 step=5 采样；勿改回逐日（EV-014） |

---

## 环境状态与验证命令

```powershell
uv run pytest tests/ -v                       # 全量测试 (242)
uv run daily                                   # 主流程（token 未配置时推送安全跳过）
uv run backtest                                # 回测：偏差 <1% + 交收差一天
uv run streamlit run dashboard/app.py          # 信号面板
```

### 项目内导入规则（EV-003）

```python
from backtest.custom_loop import run_custom_backtest
from dashboard.data_loader import load_latest_signals
from orchestration.daily_pipeline import run_daily_pipeline
```

---

## 收尾要求（本阶段结束时）

- 更新 `docs/phase0.5-audit.md`（每项状态与证据）与 `docs/task-phase0.5.md`（勾选+证据）
- 更新 `docs/PROJECT_MEMORY.md`（演化记录 EV-015+、快照、未解决问题）
- 更新 `README.md` 的实际运行/导入方式（plan M7）
- 重写本文件 HANDOFF.md 供再下一阶段；**Phase 0.5 完成不等于允许实盘，也不自动进入 Phase 1**

---

*本文件由 Session 收尾流程生成 | 请在下次会话开始时首先阅读此文件*
