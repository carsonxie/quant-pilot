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
| M4 | 目标、风险与调仓建议统一 | M2、M3 | [ ] |
| M5 | B0/B1 与主动策略对照接入 | M4 + 可信回测 | [ ] |
| M6 | 基础绩效、实验追踪与展示 | M5 | [ ] |
| M7 | 迁移演练、集成验收与交接 | M2–M6 | [ ] |

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

## 5. M4 — 目标、风险和调仓建议统一

前置：M2、M3。

- [ ] **M4-01 共用规划逻辑**：目标→与账户比较→数量（参考价/费用/最小单位/现金/可卖）→有效期与状态；策略函数只用截至决策时点数据
- [ ] **M4-02 风险优先顺序**：可用性约束→账户硬约束/暂停→风险退出与暴露上限→常规调整→成本/持有期过滤；下游不得放宽
- [ ] **M4-03 绝对百分点 band 再平衡**：越界资产调回目标；未越界不追精确对齐；整数份额/费用留残余偏离
- [ ] **M4-04 建议状态机**：卖出依赖、替代/过期、反馈迟到、重复日任务、冷却/重新进入；明确退出与首次建仓不被带宽永久跳过
- [ ] **M4-05 现金预留**：只影响可用量、不减净资产；作废/完成释放；不重复占用
- [ ] **M4-06 ATR 状态化**（C06）：持有期高水位 + 棘轮接线；重新建仓重置；停牌不可卖持续展示风险
- [ ] **M4-07 测试**：目标 20% 未变/实际 28%、昨日未执行、部分成交、现金不足、不足一手、止损 10% 不被压制、NAV 缺失、停牌持有（§11）

---

## 6. M5 — B0/B1 与主动策略对照

前置：M4 + 可信回测。

- [ ] **M5-01 B0 固定初始配置买入持有**（独立模拟账户）
- [ ] **M5-02 B1 固定目标 + 阈值再平衡**（检查日按交易日历；目标不变但漂移触发）
- [ ] **M5-03 A 主动策略接入现有目标生成器**（同一执行基础）
- [ ] **M5-04 同口径**：相同初始映射/资本/产品范围/价格/成本框架；三账户独立现金/份额/净值；B0 新增现金政策显式
- [ ] **M5-05 固定研究配置与版本记录**：不搜索最优 SAA；示例明确标注；成本双情景（基准 + 更不利）

---

## 7. M6 — 基础绩效、实验追踪与展示

前置：M5。

- [ ] **M6-01 run/experiment 记录**：代码/数据/池/配置/成本/模式/时点/结果位置/失败状态；失败试验保留（P05-D6）
- [ ] **M6-02 基础绩效**：期末权益 − 期初权益 − 净外部入金 = 投资盈亏；持仓/分红/费用贡献与差额单列（P05-D7）
- [ ] **M6-03 收益口径**：现金流口径；只有日线时现金流时点约定与近似标注；参考价滑点不重复扣现金
- [ ] **M6-04 面板扩展**（C21 只读连接）：真实/模拟标签、账户更新时间、实际/目标、现金、待处理建议、反馈入口、数据问题、净值与 B0/B1/A 对照（P05-D8）
- [ ] **M6-05 测试**：零收益 + 入金不改收益率；盈亏闭合；重复导入/重跑不改账

---

## 8. M7 — 迁移演练、集成验收与交接

前置：M2–M6。

- [ ] **M7-01 隔离副本演练**：迁移、重跑、恢复；半笔事件保护；真实事件不被重算覆盖（plan §5）
- [ ] **M7-02 关键场景证据**（plan §6 表 + §11）：对照 P05-D1–D8 记录证据
- [ ] **M7-03 文档更新**：README 实际运行/导入方式；旧报告口径标注；`task-phase0.5.md`/审计/记忆收尾
- [ ] **M7-04 已知限制与恢复路径**：列明未实现未来模块、阻塞与恢复方法；Phase 0.5 完成 ≠ 允许实盘、≠ 自动进入 Phase 1

---

## 9. 后续阶段登记（本阶段不实施）

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

---

## 10. P05-D1–D8 验收对照

| 需求 | 主要任务 | 状态 |
| --- | --- | --- |
| P05-D1 审计纠偏 | M0 初审 + M1–M7 持续关闭（`phase0.5-audit.md`） | [ ] |
| P05-D2 四类记录与账户 | M2-01…07、M4-04/05 | [ ] |
| P05-D3 ETF 池 | M3-01…06 | [ ] |
| P05-D4 基准 | M5-01/02/04 | [ ] |
| P05-D5 建议与再平衡 | M4-01…07 | [ ] |
| P05-D6 实验与版本 | M1-1-01 起步，M2-02/M6-01 完善 | [ ] |
| P05-D7 贡献与执行记录 | M2-04 采集，M6-02/03 核对 | [ ] |
| P05-D8 展示 | M6-04、M7-03 | [ ] |

## 11. 样本案例清单（spec §8.2）与覆盖任务

| 样本案例 | 覆盖任务 | 当前状态 |
| --- | --- | --- |
| 目标 20% 未变、实际漂移到 28% | M4-03/M4-07、M5-02 | 未实现 |
| 昨日建议未执行 | M2-05、M4-04 | 未实现 |
| 卖出只部分成交 | M2-04、M4-04 | 未实现 |
| 现金不足 | M1-2-03（已有引擎用例）+ M4-01 | 部分（M1 复核） |
| 重复导入 | M2-03 | 未实现 |
| 不足一手 | M1-2-03（已有引擎用例） | 部分（M1 复核） |
| 止损 10% 持仓不被旧 15% 门槛压制 | M1-1-03 | 未实现（M1 修正） |
| NAV 缺失 | M1-2-01、M3-02/05 | 部分（M1 显式化） |
| ETF 停牌但仍持有 | M1-2-03、M3-05、M4-06 | 部分 |
| 无行情收益时入金不改变收益率 | M6-03/M6-05 | 未实现 |

## 12. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-09-14 | M0：创建本文件（Phase 0 补项 A0-1…A0-6 + M1–M7 原子任务 + 验收对照 + 样本案例映射）。 |
| 2026-09-14 | M1-1 完成：C01/C04/C08 修正并验证；A0-1 部分（daily 结果已含 mode/版本）；净增 18 项测试，260/260 通过；真实回测验收保持。 |
| 2026-09-14 | M1-2 完成：C05/C06/C09/C10/C11 修正与证据；A0-3/A0-4/A0-5 完成（A0-4 限标注待 M2 复核）；新增 16 项测试，276/276 通过；真实回测 + 副本干跑验证。 |
| 2026-09-14 | M1-3 完成：推送/面板模拟标注与版本落地（schema v2 迁移）；真实库备份+迁移+副本网络重跑；§8.1 收尾核对；280/280 通过。 |
| 2026-09-14 | AGENTS.md 更新至 v3.0（用户确认）：§5.2 #2–#6、§7、§8 #4、§10 与 spec v3.0 对齐；C13 关闭。 |
| 2026-09-14 | M2a 完成：账户账本（事件/回放/更正/去重/事务）、schema v3（备份+演练+真实库迁移）、CSV+CLI 录入、runs 运行记录；28 项新测试 → 308/308。 |
| 2026-09-14 | M2b 完成：target/target_weight 落库（含现金权重/版本）、proposal/feedback 状态机与账户联动、样本案例、录入手册（README）；15 项新测试 → 323/323。**M2 全部完成**。 |
