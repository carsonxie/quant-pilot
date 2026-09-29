# task-phase0.5.md — Phase 0 收尾补项与 Phase 0.5 原子任务

> 创建：2026-09-14（M0） | 依据：`docs/spec.md` v3.0（§8.1/§8.2/§9）、`docs/plan.md` M0–M7、`docs/phase0.5-audit.md`
> 旧 `docs/task.md` 保留原样（只读，历史进度参考）；本文件为 Phase 0 收尾与 Phase 0.5 的现行任务清单。
> 状态标记：`- [ ]` 未开始 / `- [~]` 进行中 / `- [x]` 已完成 / `- [!]` 受阻
> **证据规则**：任务完成必须有可复现证据（测试名 + 命令 + 日期，或运行记录）；不以文件存在或文档勾选作为完成依据。测试修复旧公式时使用独立期望值（spec §9.1）。
> 约束：复用现有实现；保留用户改动与真实历史数据；迁移按 plan §5（git 状态→备份→隔离副本演练→半笔事件保护）；研究配置明确标注；不自动启用真实资金；不提前开发 Phase 1/1.5。

---

## 0. 批次总览

| 批次 | 工作 | 前置 | 状态 |
| --- | --- | --- | --- |
| M0 | 仓库核对、C01–C23 初审、任务拆分 | 新文档就位 | ✅ 完成（`docs/phase0.5-audit.md`） |
| M1 | A 类纠偏 + Phase 0 收尾（A0-1…A0-6） | M0 | ✅ 完成（含 AGENTS.md v3.0，2026-09-14） |
| M2 | 存储迁移、账户事件与四类记录 | M1 | ✅ 完成（M2a+M2b, 2026-09-14） |
| M3 | 数据时点与 ETF 四层池 | M2 契约 | ✅ 完成（2026-09-14） |
| M4 | 目标、风险与调仓建议统一 | M2、M3 | ✅ 完成（2026-09-15，395/395 测试） |
| M5 | B0/B1 与主动策略对照接入 | M4 + 可信回测 | ✅ 完成（2026-09-15，411/411 测试） |
| M6 | 基础绩效、实验追踪与展示 | M5 | ✅ 完成（2026-09-15，432/432 测试） |
| M7 | 迁移演练、集成验收与交接 | M2–M6 | ✅ 完成（2026-09-15，437/437 测试） |
| M8 | 权益事件登记与拆分感知质检（I-09 第一步） | M1–M7（用户确认追加） | ✅ 完成（2026-09-20，447/447 测试） |

版本标识与隔离配置自 M1 起引入；每批结束更新本文件状态、`docs/phase0.5-audit.md`、`docs/PROJECT_MEMORY.md`。

---

## 1. Phase 0 收尾补项（A0 系列，全部随 M1 关闭）

- [x] **A0-1 策略/数据版本标识**（来源：§8.1 推送证据项；C19/C20 相关）
  - 产出：`project.mode/strategy_version` 随 daily 输出（M1-1）；信号携带 `strategy_version` 列（schema v2 迁移+旧记录标注 v0.1）；推送与面板展示数据截止与版本；面板信号表"版本"列。
  - 证据：`TestSchemaMigration` 3 例、`test_header_has_mode_version_and_data_cutoff`、AppTest 字幕断言、副本混版共存记录（09-11=v0.2、旧日=v0.1）。
- [x] **A0-2 推送数据时间与失败可追踪**（来源：§8.1）
  - 产出：推送含数据截止/版本/模式；推送结果结构化日志（ok/channel/attempts/error），M2 起入库 run 记录；token 缺失安全跳过保持。
  - 证据：`test_header_has_mode_version_and_data_cutoff`；`_push_signals` 结构化日志字段。
- [x] **A0-3 回测独立手工期望值**（来源：§8.1、§9.1；C05/C08）
  - 产出：现金/份额/费用/下一可交易时段的独立手工对账用例（不复制实现输出）。
  - 证据：`test_round_trip_manual_reconciliation`（期末 109,752.51125 手工值）、`TestLotAndCashAvailability`（批次/可取/跳空/无价不成交）、M1-1 最低收费用例。
- [x] **A0-4 分红/复权限制标注**（来源：§8.1；C09）
  - 产出：`CALIBER_NOTE` 进入回测摘要与 CLI；审计记录真实拆分事件清单（513100/159941/159928/512010/512480/159995/513500/512690/516160/512100 等）。
  - 证据：`uv run backtest` 报告头；审计 C09 §真实数据核实。完整权益入账在 M2 复核。
- [x] **A0-5 批次可卖与场景证据**（来源：§8.1；C05）
  - 产出：买入批次（lot）级可卖时间；现金可用/可取分离；现金不足、无有效价格不成交、部分成交不超卖场景测试。
  - 证据：`_Lot`/`_Position` 批次模型 + `TestLotAndCashAvailability`；nav 增加 `cash_withdrawable`。
- [x] **A0-6 面板/推送模拟状态标注**（来源：§8.1；C03）
  - 产出：面板"研究/模拟模式"横幅、字幕与章节改名；推送头部"无真实账户接入，非成交/持仓记录"。
  - 证据：AppTest 横幅/字幕断言；`test_header_has_mode_version_and_data_cutoff`。

---

## 2. M1 — A 类纠偏与 Phase 0 收尾

**退出条件**：spec §8.1 证据齐备；C01/C04/C08/C10/C11 关闭，C05/C06/C09/C03 完成本阶段部分（映射见 §10）；全量测试通过；旧报告/信号标注口径。

### M1-1 策略公式与成本口径（A 类核心）✅ 2026-09-14

- [x] **M1-1-01 研究模式与版本基础设施**（依赖：无）
  - 产出：`settings.project.mode="research"` + `strategy_version="v0.2"`；`PipelineResult.mode/strategy_version` 字段并入 summary（"[模式] research | 策略版本 v0.2 | 数据截止 …"）；信号原因携带实际加权方法标识。
  - 证据：`test_real_settings_has_required_sections`（project/scenario 断言）；`test_summary_is_readable`。推送/面板接线归 M1-3-02。
- [x] **M1-1-02 C01 修正：inverse_volatility = 1/σ**（依赖：M1-1-01）
  - 产出：`PortfolioOptimizer(method=...)` 分派 1/σ（默认）/1/σ²（对照）；`method` 与结果一致；未知方法拒绝；旧行为保留。
  - 证据（独立期望值）：`TestWeightMethods` 8 例——σ={10%,20%} → 2:1 / 4:1；公开 API σ 比 2 → 权重 (0.30, 0.15)；`from_settings` 方法传递。全量 260/260。
- [x] **M1-1-03 C04 修正：退出不被防抖压制**（依赖：M1-1-01）
  - 产出：风险退出(止损)/明确退出(清零)/宏观骤降减仓 → 无条件 SELL；首次建仓绕过防抖（spec §5.4）；防抖仅约束常规调整；原因留痕。
  - 证据：`TestSignalDebounce` 6 例 + `TestRiskExitIntegration`（ATR 暴跌剔除并 SELL、无回补）。
- [x] **M1-1-04 C08 修正：成本口径与旧滑点降级**（依赖：M1-1-01）
  - 产出：`_commission_of = max(成交额×费率, 最低收费)`；`trading_cost.scenario="legacy_stress"` 命名+注释；CLI 报告输出情景名。
  - 证据：`TestCostScenarioAndMinFee` 4 例（最低收费、费率、手工往返 109,752.51125、滑点不双扣）；真实回测复跑 510300 偏差 0.044%、交收差一天 ✓。

### M1-2 数据、时序与回测证据 ✅ 2026-09-14

- [x] **M1-2-01 C10 修正：产品感知与 unknown 显式**（依赖：M1-1 完成）
  - 产出：validator 按产品限制（默认 10% 板 + overrides 20% 板 159915/588000）分级：限内 pass / 超限容差(2pp)内 warning / 超限+容差 critical（疑似数据损坏或未调整权益事件）；universe_filter 输出 `unknown`（NAV 缺失不静默）；daily 传入 settings 阈值并显示 unknown 数。
  - 证据：`TestProductAwareDailyChange` 7 例（10%/20% 板边界、容差分级、拆分型跳变 critical、validate 透传）；`test_no_nav_data_records_unknown`；真实干跑显示 19 只 unknown。
- [x] **M1-2-02 C11 修正：宽度命名与宏观 scope 显式**（依赖：无）
  - 产出："ETF 代理宽度"命名全链路（文档/摘要/面板指标）；`macro_timing.scope: a_share`（默认：宏观上限只约束 A 股合计，非A 用 `portfolio.non_a_share_max_total=0.30` 研究示例独立上限）；`all_composite` 保留为显式研究版本；daily 摘要输出 scope。
  - 证据：`TestMacroScope` 3 例（A 股 0 上限时黄金仍得 0.30；all_composite 全清零；分桶上限独立期望值）；真实数据干跑（副本）显示 scope 生效。
- [x] **M1-2-03 C05 批次可卖与场景证据**（依赖：M1-1-04）
  - 产出：批次级 `_Lot`；现金 `cash`/`cash_withdrawable` 分离；场景用例。证据：`TestLotAndCashAvailability` 5 例 + 既有交收测试批次化。
- [x] **M1-2-04 C06 独立证据：时序与跳空**（依赖：M1-2-03）
  - 产出：跳空场景断言"卖出按 T+1 开盘价成交（非假定止损价）"；ATR 研究近似（2×ATR 收盘价窗口）在 risk_manager docstring 显式标注（截断说明见审计 C06）。证据：`test_gap_executes_at_next_open_not_assumed_stop`。
- [x] **M1-2-05 C09/A0-4 分红与复权限制标注**（依赖：无）
  - 产出：`CALIBER_NOTE`（不复权、不含分红/权益事件）进入 `BacktestResult.summary()` 与回测 CLI 报告；`fetch-data` 全量导入增加产品感知质检摘要（不阻断，历史权益事件可见）。证据：回测输出头；审计 C09 记录真实拆分事件清单。

### M1-3 标注、面板与收尾 ✅ 2026-09-14（含 AGENTS.md v3.0）

- [x] **M1-3-01 C03 部分：模式与真假区分标注**（依赖：M1-1-01）
  - 产出：daily 摘要含 mode/版本/数据截止/scope；推送头部"🧪 research 模式 | 策略版本 | 数据截止 | 无真实账户接入，非成交/持仓记录"；面板顶部"研究/模拟模式"横幅、字幕"数据截止/信号策略版本/信号非成交"、表列"版本"、章节改名"策略目标持仓（模拟，非实际持仓）"。
  - 证据：`test_header_has_mode_version_and_data_cutoff`；AppTest 断言横幅与字幕；`test_signal_table_columns_and_format`。
- [x] **M1-3-02 A0-1/A0-2 数据时间与策略版本落地**（依赖：M1-3-01）
  - 产出：`trade_signals.strategy_version`（schema v2 迁移，旧记录标注 v0.1，实时库已迁移并核对内容不变）；推送与面板展示数据截止与版本；推送结果结构化留痕 `push_result ok/channel/attempts/error`（持久化运行记录 M2）。
  - 证据：`TestSchemaMigration` 3 例；`test_first_run_produces_signals` 版本断言；副本网络重跑显示 09-11=v0.2、旧日=v0.1 共存。
- [x] **M1-3-03 C13/C19 文档处置**（依赖：用户确认）
  - 产出：AGENTS.md **已更新至 v3.0**（§5.2 #2–#6、§7 #2–#5、§8 #4、§10；审计 §4.1 保留对照表）；C19 旧 task 验收作废登记（§9 既有）。
  - 证据：AGENTS.md §10 v3.0 行；用户 2026-09-14 确认记录。
- [x] **M1-3-04 策略重跑与新旧口径并列**（依赖：M1-1、M1-2 修正完成）
  - 产出：真实库备份（`data/backups/quant_pilot_20260914.duckdb` + 迁移前信号 CSV）→ 隔离副本演练 → 真实库迁移（11 条旧信号标注 v0.1，内容逐行不变）→ 副本真实网络重跑（09-11 生成 v0.2，旧日保留 v0.1）。当前无新交易日数据（最新 2026-09-11），真实库 v0.2 将在下一交易日 daily 运行自然生成（不重跑旧信号日）。
  - 证据：迁移前后 counts/sum/行内容比对输出；副本运行记录；备份文件。
- [x] **M1-3-05 M1 收尾核对**（依赖：全部 M1）
  - 产出：§8.1 证据清单逐项核对（见审计 §1 更新）；A 类相关项状态更新；PROJECT_MEMORY 记录。
  - 证据：审计 §1 全绿（推送/面板行与模拟状态行闭合）；全量 280/280。

---

## 3. M2 — 存储迁移、账户事件与四类记录（[~] M2a ✅ / M2b 待启动）

前置：M1 完成。产出：可信账户可回放，导入/反馈幂等，真实与模拟分离。

### M2a 账户层 ✅ 2026-09-14

- [x] **M2-01 事件接口契约**：见 `src/data_pipeline/accounting.py` 模块 docstring（事件类型/字段/现金可用与可取/更正语义/确定性去重）
- [x] **M2-02a schema v3（隔离副本演练 ✅）**：accounts / account_events / position_lots / account_snapshots / targets / target_weights / trade_proposals / execution_feedback / runs / experiments / schema_migration_log 已建；真实库已迁移（pre_v3 备份 → 副本演练 → 真实库，幂等且内容不变）。`instrument`/`universe_version` 归 M3（原列表在此修正）
- [x] **M2-03 统一写入口与事务**：`AccountService.import_events`（事件→回放→批次/快照同一事务；重复导入不产生第二笔；失败回滚不留半笔）；多进程并发协调文档归 M2b/M7（C21）
- [x] **M2-04a 账户事件类型**：initial/deposit/withdrawal/trade/fee/dividend_accrual/dividend_payment/share_change/correction；标准 CSV + `uv run account-import`；模板 `docs/account_events_template.csv`
- [x] **M2-05a 回放与隔离**：FIFO 批次、T+1 可取、应收不双计、账户/mode 隔离；"通知/查看不产生成交"（无此类写入路径）
- [x] **M2-06a 备份与演练**：`data/backups/quant_pilot_20260914_pre_v3.duckdb` + 副本演练 + 核对摘要（schema_migration_log.checks_json）
- [x] **M2-07a 测试（账户层）**：28 项新增（去重/回滚/隔离/更正/份额变动/分红/CSV/模板/运行记录），全量 308/308；CLI 演示：同一 CSV 两次导入 → 新增 10 / 去重 10，现金与持仓手工可核对

### M2b 四类记录其余三层 + 样本 ✅ 2026-09-14

- [x] **M2-01b target/target_weight 持久化**：daily 写 `targets`（target_id/strategy_id=five-funnel/版本/mode/as_of/decision_time/data·pool·config 版本/原因）+ 完整权重（含现金 `__CASH__`，和为 1）；池版本用标的集哈希。证据：`TestTargetRecord`（含现金权重断言）+ 真实数据副本干跑（`pool_version=universe-19etf-abc0b4d5`、weights Σ=1、cash=1.0）。
- [x] **M2-02b proposal/execution_feedback**：`src/orchestration/proposal_ledger.py`（建议去重、状态机 proposed→partial→filled + rejected/expired/replaced、部分成交累计、超额告警保留事实、反馈幂等、反馈→账户联动入账（可关）、非系统交易入账、新目标替代旧建议）。证据：`tests/test_proposal_ledger.py` 14 项。
- [x] **M2-03b 样本案例**：部分成交（600+400 两笔 → 状态与现金逐项期望值）、未执行（状态保持、不入账）、重复导入（feedback_id/event_id 双层去重）、出入金（M2a）。证据：`TestFeedbackFlow` / `TestReplayBasics`。
- [x] **M2-04b 手动录入说明与并发协调**：README"账户事件录入"节（CSV/CLI/事件类型/单通道原则/单写进程约束）；HANDOFF 同步。

---

## 4. M3 — 数据时点与 ETF 四层池（✅ 完成 2026-09-14）

前置：M2 版本/状态契约。

- [x] **M3-01 产品与规则契约**：schema v4 新增 `instruments`（交易所/市场类型/资产类别/指数/上市日近似/结算/涨跌幅限制/溢价检查必需标记/来源）与 `universe_versions`/`universe_members`（成员生效区间）与 `nav_daily`；`sync_instruments_and_pool` 从 settings+数据派生同步（幂等，内容哈希版本 `uv-…`，出池标注区间不删历史）。证据：`TestSyncPool` 3 例 + 真实库 v4 迁移（备份/演练/应用）+ 真实干跑（19 产品、池版本 uv-abc0b4d53f）。
- [x] **M3-02 四层池**：`src/strategies/etf_pool.py` — 层1 配置范围（settings+instruments）、层2 产品对照（M3-06）、层3 当日可交易池（pass/fail/unknown/not_applicable；can_buy/can_sell/can_hold 与原因；禁买≠清仓）、层4 持仓参数独立于筛选。daily 已接入 buyable 列表（`PipelineResult.passed_symbols`）。证据：`TestTradableUniverse` 7 例。
- [x] **M3-03 历史池版本**：成员生效区间 + `member_buyable_at`（上市日前不可买；版本声明日前标 `conditional_research`）；`membership_mode` 进入池报告。证据：`TestHistoricalMembership` 3 例 + 出池关闭测试。
- [x] **M3-04 代理与宽度**：M1-2 已完成命名与 scope；M3 保持（池报告含数据新鲜度，不冒称全 A）。
- [x] **M3-05 缺失与修订**：缺 NAV 且产品要求溢价检查（settings.universe_filter.premium_check_required_market_types，默认跨境）→ unknown → **禁新买**、持仓保留（真实干跑：5 只跨境禁买）；停牌/无当日价格 → 禁买禁卖、持仓保留并按最近价估值；信号层停牌持仓 **HOLD 保留原目标**（不盲目 SELL）。复权修订：以 pool/data 版本字段留痕（完整权益事件入账仍待 M2 扩展/P1）。证据：`TestTradableUniverse`（stale/nav）、`test_stale_symbol_holds_instead_of_sell`、`TestCrossBorderNavGate`。
- [x] **M3-06 产品对照与相关性诊断**：`src/strategies/product_compare.py` — 相关矩阵（共同交易日逐点对齐、报告窗口/样本/跳过原因）、同指数分组、对照表（波动/区间收益/日均额/样本标注）；不按阈值自动删池。证据：`tests/test_product_compare.py` 7 例（相同序列=1、镜像收益=−1、样本不足 skipped）。

**M3 已知限制/解锁路径**：跨境 ETF 新增买入被 NAV unknown 门槛阻止（spec §4.4 保守设计）；解锁需接入 NAV 数据（I-03，P1 或专项任务）。

---

## 5. M4 — 目标、风险和调仓建议统一（✅ 完成 2026-09-15）

前置：M2、M3。产出：`src/strategies/rebalance_planner.py`（纯规划函数）、`src/strategies/strategy_states.py`（持有期状态与冷却）、`src/orchestration/rebalance_run.py`（编排）、schema v5（`strategy_states`）、daily Step 10 接入。

- [x] **M4-01 共用规划逻辑**：`plan_rebalance()` — 目标 vs 账户实际权重 → 数量 (参考价=最近可用收盘价, 费用估计=佣金 max(率×额, 最低) + 滑点估算不重复扣现金, 100 份整手, 现金可用量) → 有效期 (valid_days) 与优先级; 账户估值 `value_account` (NAV = 现金+应收+Σ市值; 缺价 → 暂停, 不静默按 0)。证据：`tests/test_rebalance_planner.py` 30 例（独立期望值）。
- [x] **M4-02 风险优先顺序**：代码顺序 = ①可用性/池许可（stale/无价/禁买禁卖）→ ②账户硬约束（非正 NAV/缺估值价 → paused）→ ③明确风险退出与清零（不受 band/最小金额压制）→ ④常规 band 调整 → ⑤成本过滤（只约束常规）；下游 `_apply_cash_constraints` 只缩减数量, 不放宽来源约束。证据：`TestRiskExitPriority`、`TestPriorityOrder`（PRIORITY_RISK_EXIT 90 > SELL 50 > BUY 30 > BUY_DEPENDENT 10）。
- [x] **M4-03 绝对百分点 band 再平衡**：`rebalancing.band_points=0.05`（示例）; |actual−target| ≥ band → 调回目标, 未越界不追精确对齐; 整手/费用留残余偏离; 目标清零/首次建仓不受 band 跳过。证据：`TestBandRebalance`（28%→20% 卖 800 份手工值; 3pp 不动; 5pp 边界触发; 首次建仓不受阻）+ 真实数据副本干跑（510500 实际 27.4% → 卖 900 份调回 20%）。
- [x] **M4-04 建议状态机/依赖/冷却**：卖出依赖 → `depends_on=SELL:...` + `conditional=True`（现金约束阶段按执行顺序分配, 累计超可用现金的买入标依赖）; 重复日任务 → 目标 ID 按决策日确定 + 建议确定性去重（同日重跑 0 新增）; 新决策日 → 旧开放建议 `replaced`（保留事实不累计）; 有效期 → `expired`（沿用 M2b）; 冷却 → `strategy_states.cooldown_until` 阻断新买。证据：`TestCashAndQuantity::test_sell_dependency_marks_conditional`、`test_rerun_same_day_deduplicates_proposals`、`test_new_decision_replaces_previous_open_proposals`、`TestAvailabilityAndCooldown::test_cooldown_blocks_buy`。
- [x] **M4-05 现金预留**：可用量 = cash − cash_reserved（账户层预留 + 规划不消耗）; 预留不减少净资产（NAV 含预留现金）; 作废/完成后的预留释放由账户层负责（本批只读）。证据：`test_reserved_cash_not_spendable`（NAV 不变, 可买量下降）。
- [x] **M4-06 ATR 状态化（C06）**：`RiskManager.calc_atr_series`（Wilder 完整序列）; `strategy_states.update_position_states` — 高水位只在持有期 (entry_date 起) 维护, 止损线 = high−N×ATR 且棘轮只上移, 触发 → `pending_exit` 每轮持续提示直至份额归零, 卖出后进入冷却, 重新建仓重置高水位/止损; 停牌/无行情保留风险提示。schema v5 `strategy_states` 持久化。证据：`tests/test_strategy_states.py` 14 例（ATR 手工核对 109/14、棘轮、重置、冷却生命周期）。
- [x] **M4-07 测试**：spec §8.2 样本案例覆盖（目标 20% 未变/实际 28%→28% 漂移用 27.4% 真实数据与 28% 合成两路; 昨日建议未执行→重算; 部分成交→差额重算; 现金不足→缩减; 不足一手→跳过; 止损 10% 不被 band 压制; NAV 缺失→暂停/禁买; 停牌持有）。全量 **395/395** 通过（M4 新增 46 项：planner 30 + 状态 14 + 集成 2 净增）。

**M4 备注/已知限制**：
- `rebalancing.account` 为空 (null) → daily 不生成建议（保持研究输出）, 用户录入真实账户后显式配置。
- 参考价 = T 日最近可用收盘价; 下一日开盘成交价不保证 (spec §6.1)。
- 现金分配为顺序分配 (按执行顺序, 同层金额降序), 非全局最优; 差额留待下一轮, 不重复累加。
- 状态化 ATR 已在 daily 生效; 回测引擎的 ATR 状态化随 P1 统一 (现回测不接风控)。

---

## 6. M5 — B0/B1 与主动策略对照（✅ 完成 2026-09-15）

前置：M4 + 可信回测。产出：`src/backtest/baseline.py`（B0/B1 模拟器，共用账户账本路径）、`src/backtest/baseline_runner.py`（runner + 实验记录 + CLI `uv run baseline`）、`settings.baselines` 固定研究配置。

- [x] **M5-01 B0 固定初始配置买入持有**（独立模拟账户）：首日盘后按初始权重下单 → 次日开盘成交；此后不做日常权重调整。证据：`TestB0BuyAndHold`（100000 资本 50% → 买 5000 份×10、费 37.5、现金 49962.5 手工核对；价格 10→12 时 NAV=109962.5）。
- [x] **M5-02 B1 固定目标 + 阈值再平衡**（检查日按交易日历；目标不变但漂移触发）：检查日 = 每周最后交易日；用**模拟实际份额**计算漂移；越界资产整手向最近调回目标。证据：`test_drift_over_band_triggers_rebalance`（价格 10→14、目标 30% 不变 → 实际 37.5% → 检查后卖 600 份；独立期望手算）+ `test_within_band_no_trade` + `test_check_day_is_last_trading_day_of_week`。
- [x] **M5-03 A 主动策略接入现有目标生成器**（同一执行基础）：A 路径（M4 `plan_rebalance` + 建议账本 + daily）与 B0/B1 共用整手/佣金/滑点/批次可卖/现金约束约定。证据：`test_active_strategy_same_execution_foundation`（整手 100 与佣金公式跨模块一致；模拟/真实账户按 account_id + mode 隔离）。
- [x] **M5-04 同口径**：相同初始映射/资本/产品范围/价格/成本框架；三账户独立现金/份额/净值；B0 新增现金政策显式 `b0_cash_policy: hold_cash`（入金留现金不买入）。证据：`test_b0_b1_independent_accounts_same_cost`、`test_deposit_held_as_cash`（B0 入金后持股不变、现金增加）、`test_active_strategy_same_execution_foundation`。
- [x] **M5-05 固定研究配置与版本记录**：`settings.baselines`（研究示例：股 70%/黄金 10%/现金 20%，来源 docs/讨论2.md，**显式标注非真实 SAA**）；`experiments` 表记录 代码/数据/池/配置/成本情景/结果位置/状态/结论（`upsert_experiment`）；成本双情景（基准 + 更不利）由 `trading_cost` 覆盖实验（`test_adverse_cost_scenario_fees_increase`；情景名进入 experiment_id 与记录）。

**M5 真实数据干跑（副本 `data/backups/rehearsal_m5.duckdb`，2019-01-02→2026-09-11）**：
- B0: 期间收益 **+104.72%** / 最大回撤 **−31.04%** / 成交 7 笔 / 费用 **58.75 元**
- B1: 期间收益 **+102.16%** / 最大回撤 **−29.84%** / 成交 14 笔 / 费用 **112.36 元**（再平衡降低回撤、增加费用；符合"再平衡本身有效果"的对照预期）
- 入金测试（2024-06-03 同额 20000 元）：B0 留现金（政策显式）、B1 按检查日纳入配置；两账户独立净值
- 已知限制：真实数据含未调整拆分跳变（I-09）→ 拆分年前后可能触发**非策略性**再平衡（2022 年 4 笔中部分源自权益事件）；报告标注该干扰，完整修复=P1 权益入账

---

## 7. M6 — 基础绩效、实验追踪与展示（✅ 完成 2026-09-15）

前置：M5。产出：`src/backtest/performance.py`（绩效核算）、schema v6（experiments 追踪列）、`dashboard/data_loader.py` + `dashboard/app.py`（账户视图扩展）。

- [x] **M6-01 run/experiment 记录**：schema v6 补齐 `mode/started_at/finished_at/error`（副本演练 + 真实库迁移, 内容不变）; 基准 runner 写入起止时间与模式; **失败试验留痕**（`status=failed` + 错误摘要, 不删除; 测试 `test_failed_experiment_is_recorded`）; 运行记录含 rebalance 明细与推送结果（M4/M6）。
- [x] **M6-02 基础绩效**：`compute_performance` — 期末权益 − 调整起点 − 净外部入金 = 投资盈亏; 持仓/现金贡献、分红、费用分项单列; 未解释差额显式（应为 0, 非零披露）。证据：`tests/test_performance.py` 14 例（费用/分红/出金/入金/涨价场景独立期望值; 与 M5 B0 联动闭环: 报告 = +104.72% 与模拟器一致, unexplained=0）。
- [x] **M6-03 收益口径**：现金流口径（入金/出金不计收益; 分红计收益）; 日线数据现金流时点 = "日终估值点"近似并随报告标注（`CASHFLOW_TIMING_NOTE`）; 参考价滑点不重复扣现金（成本只以 fee 事件/账本进入）。证据：`TestReturnCaliber`（零收益+入金 → 收益 0; 期中入金不入分母; TWR 与简单收益一致）。
- [x] **M6-04 面板扩展**（C21 只读连接）：`_read_only` 连接（read_only=True; 不写库, mtime 不变测试）; 新增 实际 vs 目标（偏离/带宽标记）、待处理建议与反馈状态、账户净值对照（B0/B1/A 独立、首日=100）、最近运行状态与失败/推送可见。证据：`TestAccountViews` 6 例 + AppTest 区块断言（真实空库显示"未配置账户"提示）。
- [x] **M6-05 测试**：全部覆盖（零收益+入金不改收益率、盈亏闭合、重复导入/重跑不改账（M2/M5 既有）、失败试验保留、只读连接）。全量 **432/432** 通过。

**M6 真实数据干跑（副本 `rehearsal_m6.duckdb`, 2019-01-02→2026-09-11）**：
- B0 绩效报告: 期末 204,721.94 / 投资盈亏 **+104,721.94** / 收益与 TWR **+104.72%** / unexplained **0.00**（与 M5 模拟器一致）
- B1 绩效报告: 期末 202,157.43 / 投资盈亏 **+102,157.43** / 收益与 TWR **+102.16%** / unexplained **0.00**
- 面板账户视图: 摘要表 2 行（模拟(回测)）、净值对照 1,868 行（首日=100 → 末值 204.72/202.16）

---

## 8. M7 — 迁移演练、集成验收与交接（✅ 完成 2026-09-15）

前置：M2–M6。产出：`scripts/rehearsal_m7.py`（演练脚本 + JSON 报告）、审计 §5（证据汇总）、README 与实际运行方式、HANDOFF 重写。

- [x] **M7-01 隔离副本演练**（plan §5）：`scripts/rehearsal_m7.py` 在副本上完成 — ① 迁移幂等（重复 `init_database`；counts/signals 逐项不变；v6）；② **半笔事件保护**（卖超 → `AccountError` 回滚, 事件表/状态无变化）；③ 重跑去重（同事件集二次导入 0 新增）；④ **恢复路径**（备份 → 新事件 → 恢复 → 重放新事件 → 现金/份额与事件指纹一致）。报告 `data/backups/rehearsal_m7_report.json`（ALL_OK=True）; 回归测试 `tests/test_rehearsal.py` 5 例。
- [x] **M7-02 关键场景证据**：`docs/phase0.5-audit.md` §5 — plan §6 十三条场景 + spec §8.2 十样本案例逐项证据（测试名/命令/记录）; **P05-D1–D8 全部闭合**（§5.3 对照表）。已复核引用的测试名/类名存在（grep 全量校验）。
- [x] **M7-03 文档更新**：README 更新为实际运行/导入方式（账户录入、基准模拟、基准即研究示例、单写进程约束）; 旧结果口径标注沿用（trade_signals 旧记录 v0.1、基准 CALIBER_NOTE、绩效现金流口径说明）; task/审计/记忆收尾。
- [x] **M7-04 已知限制与恢复路径**：见 `HANDOFF.md` "已知限制与恢复路径" 节 — 未实现未来模块（NAV 接入 I-03、权益事件 I-09、vectorbt I-02、C02/C16/C17/C22/C23 登记 P1/P1.5）、阻塞与恢复方法、数据问题操作约束；**Phase 0.5 完成 ≠ 允许实盘、≠ 自动进入 Phase 1**。

**M7 结论**：全量 **437/437** 测试通过；真实库 schema v6（0 账户/0 建议, 研究状态）；三 Gate（工程/研究/真实账户启用）中工程 Gate 证据齐全, 真实账户启用 Gate 未触发（需用户明确启用, 且账户/前瞻运行证据另行检查）。

---

## 9. M8 — 权益事件登记与拆分感知质检（✅ 完成 2026-09-20）

来源：`uv run fetch-data` 全量质检出现 13 条 critical（未调整权益事件跳变, I-09），用户要求实施"方案1"。目标：把历史跳变从"每次全量都喊人"升级为**结构化登记 + 拆分感知质检**；不修改任何价格数据。

- [x] **M8-01 `equity_events` 登记表（schema v7）**：字段 symbol/event_date/event_type/ratio/ratio_semantics/source/inferred/verified/method/note; 副本演练（`rehearsal_v7`）+ pre_v7 备份 + 真实库迁移（counts 不变）。证据：`TestEquityEventPersistence`、迁移测试。
- [x] **M8-02 拆分感知质检**：`validate_ohlcv(registered_equity_events=...)` — 命中登记日期且比例一致 (|实际−期望| ≤ 2pp 或 5%×期望) → `registered_equity_event`（info, 不阻断）；未登记大跳变 → 维持 critical; 比例不符 → critical（警惕二次修订, 防"洗白"）。证据：`TestSplitAwareValidation` 7 例（独立期望值: ÷2.0 → −50% 放行、1.5 不符仍 critical、反向折算 +221% 放行、日期必须精确匹配）。
- [x] **M8-03 登记脚本 `scripts/register_equity_events.py`**：产品感知跳变扫描（20% 板不误报）、`--check` 列出未登记、`--apply` 登记已核验清单、`--apply --auto` 自动登记；**登记后自动复核, 同一标的残余 critical → 事务回滚**。daily Step 1 质检与 fetch-data 全量质检均已接入登记表。
- [x] **M8-04 真实库登记（12 条）**：11 条份额折算 + 512690 2021-12-31（开盘缺口证据）; 全部 `inferred=True, verified=False`（推断值待核验）。未登记项：510500 2022-08-29（−12.74%, 幅度小, 维持人工核验）。
- [x] **M8-05 测试**：`tests/test_equity_events.py` 10 例（含登记回滚、产品感知扫描）；全量 **447/447** 通过。
- [x] **M8-06 文档**：README 数据问题节、审计 I-09 状态、task 变更记录、PROJECT_MEMORY/HANDOFF。

**效果验证**：
- 全量质检: 12 条 critical → **1 条**（510500 边界项, 人工核验项）
- 拆分窗口质检（512480 2026-07-03 / 159995 2026-07-07）: 2 critical → **0 critical + 2 info**（核心目标: 增量遇拆分日不再阻断）
- daily 干跑（副本, 数据至 2026-09-18）: 正常完成
- 已知限制: 512100 因子偏大（0.362）待优先核验; 全部登记为推断值, 官方数据接入（Phase 1/方案2）后升级为 `official`。

**约束**：不修改原始价格；登记表仅作"预期跳变日历"；方案2（账户入账）在 Phase 1 实施（见 §10 登记表）。

---

## 10. 后续阶段登记（本阶段不实施）

| 登记项 | 按新 spec 的口径 | 实施阶段 |
| --- | --- | --- |
| C02 风险平价 RC | RC=w×MRC，加总闭合；旧 T1.7"MRC 均等化 <5%"验收作废 | P1.5 |
| C16 Brinson/因子归因 | 区分贡献/解释/风险/误差；不制造不可识别项 | P1.5 |
| C22 策略状态监控 | watch/paused/恢复记录；暂停≠清仓；成熟标签 | P1.5 |
| C17 RankIC/ICIR | 频率/样本/重叠处理；阈值作研究假设 | P1 |
| C23 12-1 动量/分组 | 精确区间收益；分组适配截面 | P1 |
| C15 vectorbt 辅助 | 逐点误差而非相关系数 | P1 |
| 旧 task.md 验收（T1.5 IC>0.02、T1.8 年化≥8%/夏普≥0.8、T1.9 相关>0.9999、T2 ML 增量） | 以三 Gate 替代；失败/无效结论可接受 | 登记作废/重定义 |
| 因子注册表与 scripts/validate_registry.py | 定义为主、指标引评估记录 | P1 |
| **I-09 方案2：权益事件入账（分红/拆分到账户账本）** | 用户持有期间发生权益事件时，以官方数据（或 M8 登记表推断值）生成 `share_change`/`dividend_accrual` 事件；权益只入账一次；**真实持仓前必须完成**；与数据源分红/拆分接入、验证一并实施 | **P1**（2026-09-18 用户确认：方案2 归入 Phase 1） |
| I-02 Python 3.14 vs vectorbt/alphalens | 兼容性决策（pin 3.12 或隔离环境） | P1 前置 |

---

## 11. P05-D1–D8 验收对照（✅ 全部闭合, 2026-09-15）

| 需求 | 主要任务 | 状态 |
| --- | --- | --- |
| P05-D1 审计纠偏 | M0 初审 + M1–M7 持续关闭（`phase0.5-audit.md` §2/§3） | ✅ 闭合（A 类全关闭；B/C 按批次或登记 P1/P1.5） |
| P05-D2 四类记录与账户 | M2-01…07、M4-04/05 | ✅ 闭合 |
| P05-D3 ETF 池 | M3-01…06 | ✅ 闭合 |
| P05-D4 基准 | M5-01/02/04 | ✅ 闭合 |
| P05-D5 建议与再平衡 | M4-01…07 | ✅ 闭合 |
| P05-D6 实验与版本 | M1-1-01 起步，M2-02/M6-01 完善 | ✅ 闭合（schema v6 + 失败留痕） |
| P05-D7 贡献与执行记录 | M2-04 采集，M6-02/03 核对 | ✅ 闭合（闭合差额=0） |
| P05-D8 展示 | M6-04、M7-03 | ✅ 闭合（面板扩展 + 只读连接） |

> 证据与逐项对照见 `docs/phase0.5-audit.md` §5（plan §6 十三条 + spec §8.2 十案例 + 本表）。

## 12. 样本案例清单（spec §8.2）与覆盖任务

| 样本案例 | 覆盖任务 | 当前状态 |
| --- | --- | --- |
| 目标 20% 未变、实际漂移到 28% | M4-03/M4-07、M5-02 | ✅ M4 完成（合成 28%→卖 800 份; 真实数据 27.4%→卖 900 份） |
| 昨日建议未执行 | M2-05、M4-04 | ✅ M4 完成（账户不变→同一计划重算; 同日重跑去重; 新决策日旧建议 replaced） |
| 卖出只部分成交 | M2-04、M4-04 | ✅ M2+M4（部分成交→差额按最新账户重算, 不重复累计） |
| 现金不足 | M1-2-03（已有引擎用例）+ M4-01 | ✅ M4（缩减至可负担; 依赖卖出标 conditional） |
| 重复导入 | M2-03 | ✅ M2（双层去重） |
| 不足一手 | M1-2-03（已有引擎用例） | ✅ M4（计划层跳过并保留原因） |
| 止损 10% 持仓不被旧 15% 门槛压制 | M1-1-03 | ✅ M1 修正 + M4（状态化止损不受 band/成本过滤压制） |
| NAV 缺失 | M1-2-01、M3-02/05 | ✅ M1 显式化 + M3 禁买; M4 缺估值价→暂停 |
| ETF 停牌但仍持有 | M1-2-03、M3-05、M4-06 | ✅ M3+M4（保留持仓; pending_exit 持续展示风险） |
| 无行情收益时入金不改变收益率 | M6-03/M6-05 | 未实现（M6） |

## 13. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-09-14 | M0：创建本文件（Phase 0 补项 A0-1…A0-6 + M1–M7 原子任务 + 验收对照 + 样本案例映射）。 |
| 2026-09-14 | M1-1 完成：C01/C04/C08 修正并验证；A0-1 部分（daily 结果已含 mode/版本）；净增 18 项测试，260/260 通过；真实回测验收保持。 |
| 2026-09-14 | M1-2 完成：C05/C06/C09/C10/C11 修正与证据；A0-3/A0-4/A0-5 完成（A0-4 限标注待 M2 复核）；新增 16 项测试，276/276 通过；真实回测 + 副本干跑验证。 |
| 2026-09-14 | M1-3 完成：推送/面板模拟标注与版本落地（schema v2 迁移）；真实库备份+迁移+副本网络重跑；§8.1 收尾核对；280/280 通过。 |
| 2026-09-14 | AGENTS.md 更新至 v3.0（用户确认）：§5.2 #2–#6、§7、§8 #4、§10 与 spec v3.0 对齐；C13 关闭。 |
| 2026-09-14 | M2a 完成：账户账本（事件/回放/更正/去重/事务）、schema v3（备份+演练+真实库迁移）、CSV+CLI 录入、runs 运行记录；28 项新测试 → 308/308。 |
| 2026-09-14 | M2b 完成：target/target_weight 落库（含现金权重/版本）、proposal/feedback 状态机与账户联动、样本案例、录入手册（README）；15 项新测试 → 323/323。**M2 全部完成**。 |
| 2026-09-14 | M3 完成：四层池/schema v4（真实库迁移）；23 项新测试 → 346/346；真实干跑（14 可买/5 跨境禁买）。 |
| 2026-09-15 | M4 完成：调仓规划器（账户对比/数量/现金/可卖/优先级/有效期）、风险优先顺序、绝对百分点 band、卖出依赖与替代/冷却、现金预留、状态化 ATR 止损（schema v5 `strategy_states` 真实库迁移）；46 项新测试 → 395/395；真实数据副本干跑（止损退出优先/band 调回/同日重跑去重）。下一批：M5。 |
| 2026-09-15 | M5 完成：B0/B1 基准模拟器（共用执行基础：T+1 开盘/整手/佣金/批次可卖/现金约束）、固定研究配置与标注、实验记录与结果文件、成本双情景、CLI `uv run baseline`；16 项新测试 → 411/411；真实数据副本干跑（B0 +104.72%/−31.04%，B1 +102.16%/−29.84%）。下一批：M6。 |
| 2026-09-15 | M6 完成：基础绩效核算（盈亏闭合/入金不计收益/分红费用单列/日线 TWR 近似标注）、schema v6（experiments 模式/起止/失败原因；失败试验留痕）、面板扩展（只读连接/实际 vs 目标/待处理建议/净值对照/运行状态）；21 项新测试 → 432/432；真实数据副本干跑（B0/B1 绩效报告与模拟器一致, unexplained=0）。下一批：M7。 |
| 2026-09-15 | M7 完成（Phase 0.5 收官）：隔离副本演练（迁移幂等/半笔回滚/重跑去重/备份恢复+新事件重放; `scripts/rehearsal_m7.py` + JSON 报告 + 5 项回归测试）、关键场景证据与 P05-D1–D8 全部闭合（审计 §5）、README 与实际运行方式、已知限制与恢复路径；全量 **437/437**。**Phase 0.5 完成（≠ 允许实盘, ≠ 自动进入 Phase 1）**。 |
| 2026-09-18 | 追加 M8（用户确认）：权益事件登记与拆分感知质检（I-09 方案1）——`equity_events` 登记表（schema v7）、已登记跳变不阻断/未登记仍 critical、登记脚本与真实库登记（11 条推断值待核验）；**I-09 方案2（账户入账）登记至 Phase 1**（§10 登记表）。任务清单见 §9。 |
| 2026-09-20 | M8 完成：schema v7 `equity_events`（副本演练 + pre_v7 备份 + 真实库迁移）; 拆分感知质检（登记命中→info 不阻断; 比例不符/未登记→critical）; `scripts/register_equity_events.py`（产品感知扫描/幂等登记/登记后复核回滚）; 真实库登记 12 条（含 512690 2021-12-31 开盘缺口证据）; 10 项新测试 → 全量 **447/447**。效果: 全量 critical 12→1（510500 边界项待人工核验）; 拆分窗口 2→0; daily 干跑正常。 |
