# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-14 | **分支**: `main` | **最后 Commit**: `4cd14c4 before phase 0.5`（M1-1/M1-2 改动未提交）
> **当前批次**: Phase 0.5 — **M1-1 ✅ / M1-2 ✅ 完成**（276/276 测试）；**M1-3 待启动**（标注/面板/重跑/§8.1 收尾）

---

## 新会话开局必读（按序）

1. `AGENTS.md` — 仓库最高准则（⚠️ §5.2/§7 旧条款与 spec v3.0 冲突，修订草案待用户确认 = I-07）
2. **本文件 HANDOFF.md** — 当前批次、证据、未关闭项与下一步
3. `docs/phase0.5-audit.md` — **C01–C23 逐项审计 + §8.1 剩余项**（工作状态的核心记录，随每批更新）
4. `docs/task-phase0.5.md` — A0 补项 + M1–M7 原子任务与验收（旧 `docs/task.md` 保留为历史参考）
5. `docs/spec.md` (v3.0) — 业务定义（§8 阶段验收 / §9 审计清单）
6. `docs/plan.md` (v1.0) — M0–M7 实施批次与迁移方案（§5 迁移保护 / §6 关键场景）
7. `docs/PROJECT_MEMORY.md` — EV-001~EV-015、I-01~I-08、§七常用命令

### ⚠️ 关键认知（务必先消化）

- spec §0.1 的进度快照已过时：**T0.1~T0.10 全部完成**（242/242 测试、真实推送/回测/面板已验证）。
- 但 **Phase 0 未按 spec §8.1 新口径收尾**：缺 A0-1…A0-6 六组证据（策略版本、数据时间、推送失败追踪、回测独立手工期望值、分红/复权限制标注、批次可卖/场景证据、面板模拟状态标注）。
- **A 类修正尚未开始**：C01（inverse_volatility 实为 1/σ²）、C04（15% 防抖压制小仓位退出/止损）、C08（旧滑点公式未降级、最低收费未接线）、C10（固定 20% 阈值、NAV 静默跳过）、C11（19 ETF 代理称市场宽度、A 股 cap 约束全部资产）；C05/C06/C09/C03 为部分项。详见 `docs/phase0.5-audit.md`。
- 不以文档勾选/提交信息作验收结论；测试修复旧公式时用**独立期望值**（spec §9.1）。

---

## 仓库实际状态（2026-09-14 复测）

| 项目 | 值 |
|:---|:---|
| 测试 | **276/276 通过**（`uv run pytest tests/ -q`，8.6s；M1-2 新增 16 项） |
| 数据库 | 19 ETFs / 34,073 rows / 2019-01-02→2026-09-11；`trade_signals` 11 条（3 个信号日，均为 1/σ² 旧口径 v0.1，保留不覆盖）；`account_state`/`macro_indicators` 空 |
| 未提交改动 | M1-1/M1-2 代码+测试+文档（portfolio_optimizer / daily_pipeline / custom_loop / validator / universe_filter / macro_timing / akshare_fetcher / settings / dashboard / 6 个测试文件 / docs 更新）|
| 已知数据问题 | 历史含未调整拆分跳变（I-09，10+ 处）；每日增量遇拆分日会阻断（隔离设计），修复在 M2/M3 |
| Python | 3.14.3；vectorbt 不兼容 = I-02（P1 才需解决） |
| 常用命令 | `docs/PROJECT_MEMORY.md` §七 |

---

## M1 实施要点

分组（每组"实现→验证→更新审计/任务→停靠汇报，待用户确认后再下一组"）：

- **M1-1 ✅ 公式与成本（A 类核心，2026-09-14 完成）**：C01（1/σ 修正 + inverse_variance 分离 + 独立期望值 2:1/4:1）、C04（退出/清零/止损/骤降/首次建仓绕过防抖）、C08（最低收费接线、`legacy_stress` 命名、手工往返对账）。新增 18 测试 → 260/260；真实回测偏差 0.044% 保持。
- **M1-2 ✅ 数据、时序与证据（2026-09-14 完成）**：C10（产品感知涨跌阈值 10%/20% 板 + 容差分级 + NAV unknown 显式）、C11（"ETF 代理宽度"命名 + `scope=a_share` 分桶、非A独立上限 0.30 研究示例）、C05（批次可卖 `_Lot` + 现金可用/可取）、C06（跳空独立证据 + ATR 近似声明）、C09（不复权/无分红口径标注 + 真实拆分事件清单）。新增 16 测试 → 276/276；真实回测 0.044% 保持；真实数据副本干跑验证。
- **M1-3 标注与收尾**（下一步）：C03 部分（mode=research、真假区分标注）、A0-1/A0-2（数据时间+策略版本进推送/面板、失败可追踪）、C13 草案（AGENTS.md 修订待用户确认）、策略重跑（只对新日期生成 v0.2，**不重跑旧信号日**，避免 UPSERT 覆盖）、M1 收尾核对 §8.1。

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
