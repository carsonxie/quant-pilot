# HANDOFF.md — Session 交接文档

> **生成时间**: 2026-09-29 | **分支**: `main` | **最新提交**: `4ac0638 m4 and m5 done`（M6–M8 及 Phase 1.1 全部改动**尚未提交**）
> **当前状态**: **Phase 1.1（单标的探索与诊断）全部批次 P11-M0–M6 ✅ 完成** —— 任务文件与验收报告见 `docs/task-phase1.1.md`（§11 = 交接报告）
> **下一步（由用户确认）**: Phase 1 研究（P1-R1–R6，独立追踪）；真实账户启用 Gate 未触发，不自动进入任何阶段

---

## 新会话开局必读（按序）

1. `AGENTS.md` — 仓库最高准则（v3.0）
2. **本文件 HANDOFF.md** — 收官状态、已知限制与恢复路径、下一步
3. **`docs/task-phase1.1.md` — §11 验收与交接报告**（P11-A1–A10 对照 / 在线演示 / P11-D1–D9 / 支持矩阵 / P1 独立状态 / 运行命令）
4. `docs/PROJECT_MEMORY.md` — EV-028…EV-034、I-01…I-09、§七常用命令
5. `docs/spec.md`（v3.1）— 业务定义（§8.4 = Phase 1.1；§8.3 = Phase 1 保留）
6. `docs/plan-phase1.1.md` — 本阶段实施方法（已执行完毕，历史参考）
7. `docs/task-phase0.5.md` + `docs/phase0.5-audit.md` — Phase 0.5 历史与审计

### ⚠️ 状态认知（务必先消化）

- **Phase 0.5 + M8 完成**（2026-09-20）；**Phase 1.1 完成**（2026-09-29）：面板新增"单标的诊断"模块 —— 我的持仓／观察名单／探索入口、技术风险分与三态（正常/警戒/防御）、六个月日 K 线（全史预热）、条件检查（股票财务/估值 + ETF 资料费率流动性折溢价）、诊断快照与状态变化。
- **Phase 1 研究（P1-R1–R6）全部未开始**，不被 Phase 1.1 完成注销。建议顺序：**P1-R1（含 I-09 方案2 权益入账，真实持仓前必须完成；A 回测风控对齐）→ P1-R2/R3（少量因子 + RankIC/ICIR）→ P1-R4/R5（对照/消融/时间划分/成本压力）→ P1-R6（vectorbt 或等价对照，I-02 版本决策）**。
- 真实库：schema **v8**；19 只池内 ETFs（行情至 2026-09-18，诊断刷新可推进单标的至最新）；诊断数据：`securities` 7179 行 / `diag_price_daily` 约 1.1 万行 / `diagnostic_snapshots` 5 份（演示样本）/ `watchlist` 2 条（510300、601398）；**交易类事实保持**：0 账户 / 0 建议 / 实验表不变。
- 研究规则声明：技术评分（diag-v0.1-research）与条件阈值（cond-v0.1-research）均为**研究示例**，未经有效性验证，页面已标注；缺失不视为低风险。
- **未提交改动**: M6–M8 + Phase 1.1 全部（新文件含 `src/data_pipeline/{market_data,securities,diag_data,fundamental_data}.py`、`src/factor_engineering/technical.py`、`src/strategies/{diagnosis,condition_checks}.py`、`src/orchestration/diagnosis_service.py`、`dashboard/{diagnostics_data,diagnostics_app}.py`、`config/{diagnostics,condition_rules}.yaml`、`scripts/*`、12 个测试文件、文档）。**不要丢弃**；提交前跑 `uv run pytest tests/ -q` + `uv run ruff check`。

---

## Phase 1.1 交付摘要（P11-M0–M6）

| 批次 | 交付 | 关键证据 |
| --- | --- | --- |
| M0 | 起点核对 + 任务初始化（用户确认） | task-phase1.1 §1；task-phase0.5 残留修正 |
| M1 | 36 项接口实测 + 能力表固化 | 探针脚本 + 3 份 JSON（`data/backups/probe_phase11_20260928_*.json`） |
| M2 | schema v8 五表 + 身份解析 + 统一取数/日历/权益事件 | `rehearsal_v8` 副本演练 + 真实库应用；股票口径 hfq |
| M3 | 指标库 + 冻结规则卡 + 计分/三态 + 快照状态机 + 统一服务 | 600519 警戒 37.7 / 510300 正常 23.9 / 513100 事件暂停 |
| M5 | 双模块面板 + 三入口 + 六个月 K 线 + 刷新冷却 | AppTest 双模块 + 详情快照渲染；headless health=ok |
| M4 | 条件检查（股票 9 项 / ETF 8 项）+ 名录兜底加固 | 600519 全未触发 / 601398 银行不适用 / 513100 折溢价 unknown |
| M6 | 验收对照 + 在线演示 + 回归 + 交接 | 全量 **663/663**；`m6_e2e_demo_20260929_2246.json`；`rehearsal_m7` ALL_OK |

---

## 环境与验证命令

```powershell
uv run pytest tests/ -q                      # 全量测试 (663)
uv run streamlit run dashboard/app.py        # 面板（侧边栏切换"账户与信号 / 单标的诊断"）
#   QUANT_PILOT_DB=<path> 可指向演练副本（默认 settings.storage.db_path）
uv run python scripts/rehearsal_m7.py         # Phase 0.5 隔离演练（迁移/回滚/重跑/恢复; ALL_OK=True）
uv run python scripts/rehearsal_v8.py --apply # schema v8 演练+应用（已完成, 幂等可重跑）
uv run python scripts/probe_phase11_interfaces.py   # 接口探针（可选复跑）
uv run daily / fetch-data / backtest / baseline / account-import  # 既有命令不变
```

项目内导入规则（EV-003，flat layout）：`from data_pipeline.duckdb_manager import DuckDBManager` /
`from orchestration.diagnosis_service import run_diagnosis` / `from factor_engineering.technical import compute_indicators`。

---

## 已知限制与恢复路径

### 未实现/暂缓（登记，不隐藏）

| 事项 | 现状 | 归属 |
| --- | --- | --- |
| I-02 Python 3.14 vs vectorbt/alphalens | 不兼容 | **P1 前置决策**（pin 3.12 / 隔离 / 放弃） |
| I-03 跨境 ETF 缺 NAV | 诊断折溢价 unknown（**不解除既有禁买**）；14/19 可买 | NAV 数据接入（专项） |
| I-09 权益事件 | 12 条 inferred 待核验（512100 优先）；**方案2 入账 = P1-R1**；510500 2022-08-29 边界项待核验 | P1-R1 |
| ETF 跟踪质量 / 底层估值映射 | 诊断中显式"不适用 + 暂缓原因"，不补造 | 后续按数据可得性 |
| 诊断外发推送（P11-M5-08） | 默认关闭；接线点=trigger 变化 + 既有 notifier | 可选后续 |
| 名录冷启动 | 深交所站点可能超时 → 新浪全量快照兜底（首次 ≈20s，随后缓存） | 已加固 |

### 数据问题操作约束（必须遵守）

- **拆分/权益事件阻断**: 每日增量遇未调整拆分日 → critical 阻断且不自动放行; 人工核验后登记; **禁止用全量 `fetch-data` 绕过**（I-09）。
- **单写进程约束**: daily / account-import / 基准模拟 / 迁移演练 / 诊断刷新写库同一时间只运行一个（DuckDB 单写；面板读取为只读连接，跨进程安全）。
- **推送 token**: 仅环境变量 `PUSHPLUS_TOKEN`。
- 诊断链**不写**账户事件/目标/建议/执行反馈/策略池成员（有测试与演示证据）。

### 恢复方法（演练已验证，plan §5）

1. 备份: `data/backups/quant_pilot_<date>_pre_*.duckdb`；事件渠道 CSV 为真实事实原始凭据。
2. 恢复: 从备份恢复后**必须重放备份点之后全部真实事件**并核对指纹（`scripts/rehearsal_m7.py` 复现该流程）。
3. 迁移: 任何 schema 变更先在隔离副本演练（`rehearsal_v8.py` 模式）；真实库应用后核对 counts 不变。
4. 重跑: 同日重跑确定性去重（信号/建议/事件/诊断快照均以确定性 ID），不重复计账。

---

## 不要重复的坑（会话交接必读）

| 坑 | 说明 |
| :--- | :--- |
| 读写连接混用（同进程） | 同一进程内已持读写了某 DuckDB 文件时，不能再开 `read_only` 连接（会报 configuration 冲突）；读取层函数须在写连接关闭后调用（测试/脚本同理） |
| 旧测试"实现自证" | 修正公式时测试改为独立期望值（spec §9.1） |
| 不可无痕覆盖 | 旧信号/建议/基准结果/诊断快照保留版本与口径标注 |
| DB 备份 | 不复制正在写入的库；迁移前停止写入或一致方式（plan §5） |
| 推送 token | 只走环境变量；不写配置与文档（EV-012） |
| Windows GBK 控制台 | CLI 入口 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` |
| ruff 基线 | 旧文件存量 UP045/UP035/B007；新代码 `X \| None` 零新增 |
| 宏观重算性能 | 面板 step=5 采样，勿改回逐日（EV-014） |
| 拆分/权益事件阻断 | 每日增量遇未登记拆分日阻断且不自动放行；勿全量 fetch-data 绕过（I-09） |
| 单写进程 | daily / account-import / 基准模拟 / 迁移演练 / 诊断刷新同一时间只运行一个 |
| 名录接口脆性 | `stock_info_a_code_name` 依赖深交所站点（实测超时）→ 已加新浪快照兜底；不要在兜底下再叠加重试风暴 |
| pandas NaN | 报表解析统一 `parse_float`（NaN/Inf→None），勿直接把 `or 0.0` 用于可能为 NaN 的值 |
| YAML 科学计数 | PyYAML(1.1) 不解析 `5.0e6`（需带符号或写全数字）；规则卡用整数/小数全写 |
| 真实库可变状态 | 针对真实库的 UI/数据测试不要假设"名单为空/无账户"永远成立；用 `QUANT_PILOT_DB` + 临时库隔离断言 |
| 测试值手工重算 | 改测试期望先手算（整手/最低收费/顺序分配/起点日约定会改变结果） |
| 不提前开发 | 不启动 Phase 1 研究/Phase 1.5/Phase 2；新阶段需用户确认 |

---

## 下一步（由用户确认）

- **Phase 1 研究**（规划见 `docs/task-phase1.1.md` §6 与 `docs/plan.md` §8）：先 P1-R1（I-09 方案2 权益入账 + A 回测风控对齐），再 P1-R2/R3 因子与 RankIC/ICIR，R4/R5 对照与成本，R6 独立对照（先做 I-02 决策）。**不自动开始。**
- 可选准备：录入真实账户（`account-import`）写入 `settings.rebalancing.account`，观察 paper 运行（真实账户启用 Gate 的前瞻证据）。
- 可选：打开面板体验单标的诊断（真实库已有 5 份演示快照与 2 条观察名单，可直接查看）。
- 待核验清单：`equity_events` 12 条推断值（优先 512100）；510500 2022-08-29 边界项。

---

## 收尾说明

- Phase 1.1 完成 ≠ 允许实盘、≠ 自动进入 Phase 1：真实账户启用 Gate 未触发（需用户明确启用 + 账户核对 + 前瞻运行证据）。
- 提交建议：`uv run pytest tests/ -q`（663）+ `uv run ruff check`（新文件零告警，存量除外）后统一提交。
