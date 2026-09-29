# task-phase1.1.md — Phase 1.1 原子任务、接口调查与验收证据

> **生成**: 2026-09-26（Coding Agent） | **状态**: **Phase 1.1 全部批次（P11-M0–M6）✅ 完成（2026-09-29）**；验收与交接报告见 §11；后续进入 **Phase 1 研究（P1-R1–R6，独立追踪）**，不自动启用实盘
> **需求依据**: `docs/spec.md` v3.1 §8.4（P11-D1–D9、P11-A1–A10）
> **实施依据**: `docs/plan-phase1.1.md` v1.0（批次 P11-M0–M6）
> **起点证据**: 本文件 §1（2026-09-26 实测：全量测试 447/447 通过；schema v7；19 ETFs / 34,168 行）
> **维护规则**: 每完成一个任务 → 同步更新本文件状态与证据；每完成一个批次 → 追加 §11 变更记录并更新 `docs/PROJECT_MEMORY.md`。
> **状态标记**: `- [ ]` 未开始 / `- [~]` 进行中 / `- [x]` 已完成 / `- [!]` 受阻（注明原因与所需帮助）
>
> **与历史任务的关系**: 本文件所有 `P11-*` 任务均为 Phase 1.1 **功能/工程任务**；Phase 1 研究任务（P1-R1–R6）在 §6 **仅独立登记状态与依赖**，不由本阶段勾选或验收。历史 `task-phase0.5.md`、`task.md` 不覆盖。

---

## 1. 起点核对：已有实现 vs 待补内容（2026-09-26 实测）

### 1.1 实测事实

| 项目 | 实测值 | 证据 |
| :--- | :--- | :--- |
| 测试基线（M0 起点） | **447/447 通过**（31.6s） | `uv run pytest tests/ -q`（2026-09-26 本会话实测） |
| 测试基线（M2 完成） | **518/518 通过**（40.4s；M0–M2 净增 71 项） | `uv run pytest tests/ -q`（2026-09-28 实测） |
| 测试基线（M6 完成） | **663/663 通过**（78.3s；M0–M6 净增 216 项） | `uv run pytest tests/ -q`（2026-09-29 实测） |
| 数据库 | schema **v8**（v7 起点）；19 ETFs / 34,168 行 / 2019-01-02→**2026-09-18**（真实库；副本冒烟可将单标的增量至 09-28） | DuckDB 只读查询 + `rehearsal_v8_report.json` |
| 账户/建议/实验 | 0 账户 / 0 建议 / 0 实验（研究状态） | 同上（accounts/trade_proposals/experiments 计数为 0） |
| 权益事件 | `equity_events` 12 条（全部 `inferred=TRUE, verified=FALSE`） | M8 / EV-027；510500 2022-08-29 边界项未登记 |
| instruments | 19 只 ETF 元数据（含 exchange/market_type/asset_class/index_name/list_date） | 本会话查询 |
| 环境 | Python 3.14.3；akshare 1.18.64；streamlit 1.59.2；**altair 6.2.2 已装**；**plotly 未装**（p1 可选组；dry-run 可装 7.1.0） | 本会话验证 |
| 工作区 | M6+M7+M8 改动**未提交**（见 HANDOFF.md）；main 分支 | `git status`（本会话） |
| 文档残留 | `task-phase0.5.md` 两处已由 P11-M0-02 修正 ✅ | P11-M0-02 |
| `factor-check` | pyproject 声明 `scripts.validate_registry:main`，**该脚本不存在**（悬空引用，归 P1-R2） | 本会话核对 |

### 1.2 可复用清单（不重复开发）

| 能力 | 位置 | 本阶段用途 |
| :--- | :--- | :--- |
| DuckDB 单写事务 / 幂等 UPSERT / 只读连接 | `data_pipeline/duckdb_manager.py`（1980 行，v7） | schema v8 迁移、诊断缓存与快照写入、面板只读 |
| 数据质检（产品感知涨跌幅 + 权益事件感知） | `data_pipeline/processors/validator.py` | 按需更新的入库前质检（复用同一护栏，不绕过） |
| ETF 日线获取（Sina 主力，不复权；eastmoney hfq 备用） | `data_pipeline/fetchers/akshare_fetcher.py` | 池内 ETF 增量更新路径参考；股票路径需新增 |
| PriceSeries（≤日最近价、交易日并集） | `data_pipeline/market_data.py`（65 行） | 估值与日历近似基础；诊断需完整 OHLC 访问 |
| ATR Wilder 完整序列 | `strategies/risk_manager.py::calc_atr_series`（与 `_calc_atr` 同源） | 诊断 ATR14% 直接复用，不重写 |
| ETF 产品契约与分项检查（pass/fail/unknown/not_applicable；禁买≠清仓） | `strategies/etf_pool.py`、`instruments` 表 | ETF 产品检查（M4）复用检查范式与元数据 |
| 账户事件回放 / 快照 / 估值 | `data_pipeline/accounting.py`、`strategies/rebalance_planner.py::value_account`、`account_snapshots` | 「我的持仓」读取（只读，不改账本） |
| 绩效与净值序列（现金流口径） | `backtest/performance.py` | 本阶段不新增计算，仅参考口径标注方式 |
| 实验表（含失败留痕） | `experiments` 表 | 接口调查若产生实验，可选择性登记 |
| 面板单页 + 只读数据层 + AppTest 测试模式 | `dashboard/app.py`、`dashboard/data_loader.py`、`tests/test_dashboard.py` | 诊断页面在同一入口扩展，沿用只读连接与测试方式 |
| 迁移保护流程（备份→副本演练→幂等→counts 核对） | `scripts/rehearsal_m7.py`、`schema_migration_log` | schema v8 按同一流程执行 |
| 权益事件登记表与拆分感知质检 | `equity_events` 表、M8（EV-027） | 诊断价格暂停/降级规则的输入 |

### 1.3 待补清单（本阶段范围）

| 缺口 | 说明 | 对应任务 |
| :--- | :--- | :--- |
| 股票行情路径 | 现有 fetcher 仅 ETF；`_to_sina_symbol` 按 ETF 前缀假设，股票代码会误判交易所 | P11-M1-01、P11-M2-01/03 |
| 证券身份解析 | 无"代码→交易所/类型/名称"服务；无歧义/错误码处理 | P11-M1-02、P11-M2-01 |
| 非池标的行情缓存 | `ohlcv_daily` 仅 19 只池内标的；探索/观察标的无处落库 | P11-M2-02/03（`diag_price_daily`） |
| 指标体系函数 | `factor_engineering/` 为空；无 MA/RSI/布林/12-1/波动率分位 | P11-M3-01 |
| 计分与规则卡 | 无诊断规则、分档、解释模板 | P11-M3-02/03/04 |
| 观察名单 / 诊断快照 | 无表、无服务、无状态变化记录 | P11-M2-02、P11-M3-05、P11-M5-01 |
| 基本信息获取 | 无股票财务/估值、无 ETF 规模/费率/净值/折溢价数据 | P11-M1-04/05/06（实测）、P11-M4-01/02/03（条件实现） |
| 交易日历 | 无独立日历（现用价格日期并集近似） | P11-M1-03、P11-M2-05 |
| 图表能力 | plotly 未装（spec §2 选型为 Streamlit + Plotly；stdlib 仅 streamlit 原生图） | P11-M5-04（选型记录在 §5.1） |
| 文档残留 | task-phase0.5.md 两处状态残留（见 §1.1） | P11-M0-02 |

### 1.4 已知限制与对本阶段的影响

| 编号 | 限制 | 本阶段处理 |
| :--- | :--- | :--- |
| I-09 | 历史行情含未调整权益事件跳变；12 条推断登记未核验；完整入账在 Phase 1 | 诊断按 spec §8.4.3-5：**未核验事件污染窗口暂停相关指标/评分并说明**，不改原始价格、不编造权益（P11-M2-04） |
| I-03 | 跨境 ETF 缺 NAV → 既有禁新买规则 | 诊断展示 NAV unknown 与限制；**不解除、不替换既有禁买规则**（P11-M4-03） |
| I-02 | Python 3.14 与 vectorbt/alphalens 不兼容 | 本阶段不需要两者；诊断复用现有计算栈（pandas/polars/numpy） |
| — | 单写进程约束（daily / account-import / 基准模拟同一时间只运行一个） | 诊断刷新走受控写入口并声明该约束（P11-M2-03、P11-M5-05） |

### 1.5 工作区纪律（实现期间必须遵守）

- M6+M7+M8 未提交改动**不得丢弃**；本阶段新增改动与之共存，提交前跑全量测试 + ruff。
- 任何 schema 变更：先 `data/backups/` 备份 + 隔离副本演练（plan §5），再应用真实库并核对 counts。
- 诊断链路（查询/评分/刷新/快照）**不得**产生 account_event、目标、交易建议或执行反馈；不得修改既有账户链路。
- 研究示例参数（评分权重/阈值等）必须显式标注"研究示例"，冻结后再评估，不得宣称已获用户确认或已验证有效。
- 测试用独立期望值手工核对，不以实现输出自证（spec §9.1）；新增代码 ruff 零告警（存量 UP045/UP035/B007 除外）。

---

## 2. 阶段边界：Phase 1.1（功能） vs Phase 1（研究）

| 维度 | Phase 1.1（本文件） | Phase 1（§6 独立追踪） |
| :--- | :--- | :--- |
| 目的 | 正确描述单标的"现在"：指标、评分、三态、条件检查 | 评估规则"对未来是否有用"：RankIC/ICIR、净收益、消融 |
| 交付 | 三入口诊断页、观察名单、6 个月 K 线、规则卡与快照 | 少量因子注册与评估、B0/B1/A 对照、时间划分、成本报告 |
| 验收 | P11-A1–A10（工程正确性、降级正确、无副作用） | P1-R1–R6（研究完成、失败披露、口径正确） |
| 不要求 | 不要求 IC/收益门槛；不要求评分有效 | 不要求所有候选有效；允许无效结论 |
| 共享内容 | 精确 12-1 动量公式实现、数据契约、权益事件登记、交易日历 | 同左（可复用本阶段成果，但独立验收） |
| 关键红线 | 无买入/候选入场/机会排名；无 LLM；无假设买后组合；不写账户 | 不因 P11 完成而注销；I-09 方案 2 仍归 P1-R1 |

**本阶段明确排除**（spec §8.4.8、plan-phase1.1 §6）：LLM API、公告/新闻理解、全市场扫描、股票交易建议/回测、自动下单、机会策略、假设买后组合、成分穿透、行业重叠/相关性判断、复杂归因、策略治理、ML。既有账户计算、ETF 筛选、风控、产品对照与相关性功能**保留不删除**。

---

## 3. 批次总览（P11-M0–M6）

| 批次 | 工作 | 前置 | 退出条件 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| P11-M0 | 仓库差异与任务初始化 | 文档到位 | 实际起点、复用/新建边界、本任务文件可审阅 | [x] 完成 2026-09-26 |
| P11-M1 | **接口可用性与数据契约调查（先行）** | M0 | 分项实测能力表 + 降级决策（附录 A） | [x] 完成 2026-09-28 |
| P11-M2 | 证券识别、可靠价格及统一取数 | M1 核心数据通过 | 身份/日期/价格/权益契约、增量缓存、schema v8 | [x] 完成 2026-09-28 |
| P11-M3 | 指标、计分、解释与快照 | M2 | 指标函数 + 规则卡 + 诊断结果与状态变化 | [x] 完成 2026-09-29 |
| P11-M4 | 条件基本面 / ETF 产品检查 | M1 调查、复用 M2 契约 | 已承诺子项有来源/计算/限制；其余暂缓登记 | [x] 完成 2026-09-29 |
| P11-M5 | 三入口、观察名单与六个月 K 线 | M2/M3；按 M4 可用结果渐进接入 | 可用 Streamlit 页面 + 刷新 + 名单管理 | [x] 完成 2026-09-29 |
| P11-M6 | 必要回归、可用性验收与交接 | 核心流程与已承诺条件子项完成 | P11-A1–A10 证据、范围矩阵、已知限制 | [x] 完成 2026-09-29 |

**执行顺序**（按依赖）：M1 → M2 → M3 →（M4 与 M3 并行推进，按 M1 结论裁剪）→ M5 → M6。M4 条件数据不可用不阻塞 M2/M3/M5 的可靠技术诊断页面。

---

## 4. 原子任务清单

### P11-M0 — 仓库差异与任务初始化

- [x] **P11-M0-01 仓库基线与差异核对**（2026-09-26 完成）
  - 目标：核对 Git 状态、测试、schema、关键模块与 upload 记录的一致性，确定复用/新建边界。
  - 产出：本文件 §1（实测事实、可复用、待补、限制）。
  - 验收：447/447 实测通过；schema v7 / 19 ETFs 与 HANDOFF 记录一致；差异清单逐项有出处。
  - 证据：本会话命令与查询结果（见 §1.1）；未丢弃任何未提交改动。

- [x] **P11-M0-02 历史任务文档残留同步**（2026-09-26 完成）
  - 目标：修正 `docs/task-phase0.5.md` 两处与完成记录矛盾的状态残留（L106 M2 标题" M2b 待启动"；L278 入金样本行"未实现（M6）"），使其与 M2b/M6 完成记录一致（spec §0.1、plan §8 要求）。
  - 依赖：无（纯文档，对照测试名/证据后再改）。
  - 产出：更新后的 task-phase0.5.md（仅状态行，不改历史事实）。
  - 验收：两处残留更新；无其他内容改动；diff 可审阅。
  - 证据：`## 3. M2 …（✅ 完成 2026-09-14, M2a+M2b）`（与批次总览 L17、变更记录 L290 一致）；样本案例行 → `✅ M6 完成（零收益+入金 → 收益率 0; test_zero_price_return_deposit_does_not_raise_return）`（TEST 名实测存在）。

- [x] **P11-M0-03 本任务文件生成**（2026-09-26 完成）
  - 产出：`docs/task-phase1.1.md`（本文件），含接口调查与共享修正先行、条件任务依赖 M1 结论的编排。
  - 验收：任务含目标/依赖/产出/验收/状态；Phase 1 研究任务独立登记（§6）；不预先声称任何 API 可用。

- [x] **P11-M0-04 用户确认任务清单（Gate）**（2026-09-26 用户确认）
  - 目标：用户审阅本文件；确认后冻结 M1 范围并开始实施；如有范围调整在此更新。
  - 证据：用户回复"task-phase1.1.md 没问题"并指示开始实施 M0。M1 范围即本文件初稿，无调整。

### P11-M1 — 接口可用性实测（先行批次）✅ 完成 2026-09-28

> 纪律：全部结论以本项目环境实测为准（调用日期/代表标的/样本范围/缺失或错误行为）。文档存在、单次成功、第三方声称均不构成可用性证明。网络受限时如实标"未实测"并登记；不得用模拟响应冒充在线证据；不做无目的反复抓取（每接口抽样 1–3 次/标的，含失败样本）。
>
> **实测工具**：`scripts/probe_phase11_interfaces.py`（可复跑，分组 `core/identity/calendar/fundamental/valuation/etf/extra/extra2`）。原始输出：
> `data/backups/probe_phase11_20260928_231728.json`（22 项）、`probe_phase11_20260928_232009.json`（10 项补测）、`probe_phase11_20260928_232343.json`（4 项资料/费率补测）。结论汇总与逐行证据见**附录 A**。

- [x] **P11-M1-01 探针工具与核心行情实测**（2026-09-28 完成）
  - 结果：ETF 日线 `fund_etf_hist_sina` ✅（510300: 3486 行 2012-05 起；513100 跨境 3200 行；最新 2026-09-28；0.3–0.6s；不复权）。股票日线 `stock_zh_a_daily`（sina）✅（600519 raw/qfq 6013 行；短历史 688981 自 2020-07 共 1500 行；含 outstanding_share/turnover；0.3–0.4s）。Eastmoney 备用 `stock_zh_a_hist` ✅ 但**偶发断连**（1 次成功 178 行 / 1 次 RemoteDisconnected）。
  - 失败行为：错误代码 → ETF 空表（需归一为"不可查询"）；股票 `KeyError('date')`（需服务层捕获，不得静默当成功）。
  - 证据：探针 JSON + 附录 A（ETF/股票日线行）。
  - 验收：通过（每种行情有真实调用、失败模式明确）。

- [x] **P11-M1-02 证券身份 / 名称解析实测**（2026-09-28 完成）
  - 结果：`stock_info_a_code_name` ✅（5569 行，code+name，含北交所 920xxx）；`fund_etf_category_sina` ✅（1687 行 ETF，sh/sz 前缀+买卖价/成交额快照）；`fund_name_em` ✅（27991 行全基金名录，辅助）；`stock_individual_info_em` ⚠️ 偶发 RemoteDisconnected（不作为唯一依赖）。
  - 边界：名称搜索路径可用（股票全表 + ETF 列表）；同名歧义未专门抽样，服务层按"多候选返还 + 用户确认"设计（M2-01）；上市/退市状态未在候选接口验证 → 登记限制。
  - 证据：探针 JSON + 附录 A（身份/名录行）。

- [x] **P11-M1-03 交易日历实测**（2026-09-28 完成）
  - 结果：`tool_trade_date_hist_sina` ✅（8797 行，1990-12-19 → 2026-12-31，含未来交易日；0.1s）。可支撑"最近已完成交易日"与新鲜度判定（周末/节假日不误判为延迟）。
  - 证据：探针 JSON + 附录 A（日历行）。验收：可用，无需降级方案。

- [x] **P11-M1-04 股票财务数据实测**（2026-09-28 完成）
  - 结果：三大报表 `stock_financial_report_sina` ✅——利润表 103 行（600519，1998→2026-06-30）、资产负债表 103 行、现金流量表 99 行；**含"报告日 / 公告日期 / 币种 / 是否审计"**（披露日可得）。金融企业样本 601398 利润表 ✅（结构与工商企业不同，证实"不适用"识别必要）。`stock_financial_analysis_indicator` ✅（86 列，受 `start_year` 控制，14 行/3 年）。
  - 口径注意：数值为**字符串**且缺失为 `'None'` 文本（非 NaN，需转换）；单位为元；净利润等年累计口径需按报告日识别。
  - 限制：公告日期可用于展示披露期；历史 PIT 研究需版本化快照（本阶段不做回测注入，登记）。
  - 证据：探针 JSON + 附录 A（财务行）。

- [x] **P11-M1-05 股票估值数据实测**（2026-09-28 完成）
  - 结果：首选 `stock_value_em` ✅（日频 13 列：PE(TTM)/PE(静)/市净率/总市值/市现率/市销率等；600519 自 2018 起 2121 行）；备用 `stock_zh_valuation_baidu` ✅（PE/PB 近一年各 366 行）。`stock_a_indicator_lg` ❌ **本版本不存在**（改用上述，登记）。
  - 边界：负 PE/负净资产/异常 PEG 会真实出现（样本含 PEG −4.6）→ 检查规则必须处理"负值不解释为便宜"。
  - 证据：探针 JSON + 附录 A（估值行）。

- [x] **P11-M1-06 ETF 扩展字段实测**（2026-09-28 完成）
  - 结果（文件口径）：
    - 资料/费率/规模/跟踪标的 → `fund_overview_em` ✅（20 列：成立日期、净资产规模截止 2026-06-30、份额规模、**管理费率 0.15%/年、托管费率 0.05%/年**、跟踪标的、业绩比较基准；510300/513100 均返回）。
    - 净值历史 → `fund_open_fund_info_em(indicator='单位净值走势')` ✅（510300：3502 行，2012-05-04→2026-09-28）。
    - 净值+折溢价快照 → `fund_etf_fund_daily_em` ✅（1669 行，含单位/累计净值、市价、**折价率**）；⚠️ 部分行为 `---`，**跨境 513100 当日净值缺失**（折价率同缺）→ 实现必须校验净值与价格日期匹配，否则 unknown。
    - 规模份额 → `fund_etf_scale_szse` ✅（1060 行）；`fund_etf_scale_sse` ⚠️ 部分（2025-01-15 可得 593 行，**近期日期 2026-09-25 失败**，需运行时探测最近可用统计期）。
  - 接口故障（登记，均有替代）：`fund_etf_fund_info_em` ❌ ValueError（本版本损坏）；`fund_etf_spot_em` ❌ 代理环境 ProxyError（`fund_etf_spot_ths` ✅ 替代）；`fund_etf_hist_em` qfq 本次 ConnectionError（EM 偶发）；xq 雪球资料接口 ❌ KeyError。
  - **暂缓项**（明确记录，不阻塞核心交付）：跟踪质量（缺可比全收益指数收益，不以价格指数/高相关替代）、ETF 底层估值映射（缺可靠 ETF→指数估值映射，黄金/债券不适用）、ETF 费率替代路径 `fund_fee_em` 空表（不适用，费率以 `fund_overview_em` 为准）。
  - 证据：探针 JSON + 附录 A（ETF 行）。

- [x] **P11-M1-07 能力表固化与降级决策**（2026-09-28 完成）
  - 产出：附录 A 已按 spec §8.4.3-2 字段逐行填实（无"文档写了未实测"的行）；失败情形单列。
  - **M4 裁剪决策**：盈利/现金流/偿债（Sina 报表 + 指标）→ 实现；估值（`stock_value_em` 主 + 百度备用）→ 实现；ETF 资料/费率/规模（`fund_overview_em` + 交易所份额）/成交条件（日线 + 新浪快照）/净值与折溢价（快照需日期匹配）→ 实现；跟踪质量、底层估值 → **暂缓并登记**。
  - **对 M2/M5 的输入**：取数选型（ETF=新浪日线；股票=新浪日线 qfq 为主、EM 备用）；日历=新浪交易日历；身份=股票全表 + ETF 列表；NAV 校验规则（日期匹配否则 unknown）。

### P11-M2 — 证券识别、可靠价格与统一取数 ✅ 完成 2026-09-28

> 交付：`src/data_pipeline/securities.py`（身份解析）+ `src/data_pipeline/diag_data.py`（日历/取数/事件）；schema v8 五表；settings `diagnostics` 节。
> 证据：新增 71 项测试（storage 18 / securities 17 / diag_data 36）→ 全量 **518/518**；`scripts/rehearsal_v8.py`（副本演练 + 真实库应用，报告 `data/backups/rehearsal_v8_report.json`）；真实数据副本冒烟（`rehearsal_m2.duckdb`，见 M2-03/05 证据）。

- [x] **P11-M2-01 证券身份解析服务**（2026-09-28 完成）
  - 产出：`securities.py` — `SecurityResolver.resolve/search`、前缀推断（纯函数）、名录缓存（`securities` 表，TTL 内不重复联网、未命中且过期时一次强制重载）、`resolver_from_settings`（读 `diagnostics.securities_cache_ttl_days`）。
  - 证据：`tests/test_securities.py` 17 例（规范化/交换所属性/名录加载一次/未知代码有界报错/刷新不重复/搜索/字段回退）+ 真实副本冒烟（600519/513100 解析成功，`search("茅台")` → 600519；513100 market_type 由 instruments 回填"跨境"）。
  - 验收：通过 — 身份以名录为准不猜测；探索/名单操作不写 instruments/universe_members（storage 隔离测试覆盖）。

- [x] **P11-M2-02 schema v8 迁移（诊断五表）**（2026-09-28 完成）
  - 产出：`securities` / `watchlist` / `watchlist_changes` / `diag_price_daily` / `diagnostic_snapshots` + CRUD（含观察名单幂等与变更记录、快照确定性 ID）；迁移 v8 块 + `schema_migration_log`。
  - 证据：`tests/test_diag_storage.py` 18 例（幂等/修订/软删除/替换/FK 语义/隔离）；`scripts/rehearsal_v8.py`：副本两次 init 幂等、23 张既有表 counts 稳定、真实库应用（versions [1..8]，counts 稳定，`apply.ok=true`）；备份 `data/backups/quant_pilot_20260928_pre_v8.duckdb`。
  - 验收：通过 — 既有表零变化；幂等；只增表。

- [x] **P11-M2-03 统一取数与更新服务**（2026-09-28 完成）
  - 产出：`diag_data.py` — `ensure_research_prices(refresh=none/auto/force)`：池内 ETF 读/写 `ohlcv_daily`（含出池旧持仓判定：ohlcv 有历史即池内路径）、非池标的读写 `diag_price_daily`（股票 hfq **全量替换**避免新旧前复权拼接，ETF 不复权增量 UPSERT）；超时/重试/退避（KeyError 已知失败不重试）、请求去重（新鲜度优先）、失败保留旧数据（stale/failed + notes）。
  - 设计修正（记录理由）：股票口径由任务原稿的 qfq 改为 **hfq（后复权）** — 历史稳定可增量、与 EV-008 标准一致；qfq 每次分红后历史重算会触发拼接污染（spec §8.4.3-4）。
  - 证据：`tests/test_diag_data.py` 36 例（首发/新鲜/过期/只读/强制/失败保留/阻断不写/增量不重复/池内更新/短历史提示/无新增）；真实副本冒烟：600519 → `updated` 6013 行（含全历史）；513100（池内）→ `updated` 写入 `ohlcv_daily` 至 2026-09-28。
  - 验收：通过 — 注册权益事件感知质检并入刷新门；critical 阻断不写入（测试构造 +60% 跳变）；全链路不写交易类表。
  - 单写协调说明：与 daily/account-import 等写进程并发时，由 DuckDB 文件锁**显式报错**（不静默损坏、事务回滚）；面板读连接不受影响（既有只读测试保持绿）。UI 层错误呈现归 M5-05。

- [x] **P11-M2-04 价格口径与权益事件处理**（2026-09-28 完成）
  - 产出：`check_equity_events`（ok / paused / data_issue / not_applicable）+ `detect_unregistered_jumps`（产品感知阈值，登记事件 ±1 日抑制）+ `adjust_prices_for_events`（全部已核验时按 ratio 链生成连续序列；仅调 OHLC，量额保持原始口径并声明）；价格口径写入 `price_version`（M3 修正为 `source|adjustment`；诊断日期由快照 `as_of` 承载）。
  - 证据：合成测试（未核验→paused、未登记跳变→data_issue、已核验→调整后收益连续、hfq 股票 not_applicable）；真实数据：513100 窗口含 1 条未核验事件 → `paused`（正是 M3 需暂停跨事件指标的场景）；600519 → `not_applicable`。
  - 验收：通过 — 原始价格不被修改；不经诊断链改写 `equity_events`；暂停/调整路径均有测试。

- [x] **P11-M2-05 交易日历与"最近已完成交易日"**（2026-09-28 完成）
  - 产出：`load_trading_calendar`（sina + 进程缓存 + **池内日期并集回退**）、`last_completed_trading_day`（本地时间 cutoff 18:30 可配；周末/节假日不误判）。
  - 证据：测试覆盖 cutoff 前后/周末/休市缺档/空日历/回退/缓存；真实冒烟：`tool_trade_date_hist_sina` 8797 天、`last_completed=2026-09-28`。
  - 验收：通过。

- [x] **P11-M2-06 无交易副作用与隔离保证**（2026-09-28 完成）
  - 产出：存储层（`TestTradingChainIsolation`）与服务层（`TestNoTradingSideEffects`）测试：securities/watchlist/diag_price/snapshot 写入 + 取数 + 事件检查全链路，账户/事件/目标/建议/反馈/universe_members/instruments/strategy_states 计数前后均为 0。
  - 证据：上述测试 + 真实副本冒烟（accounts/trade_proposals 0 → 0；universe_members 19 → 19）。
  - 验收：通过 — 面板既有只读测试保持绿（518/518 全量）。

### P11-M3 — 指标、计分、解释与快照 ✅ 完成 2026-09-29

> 交付：`src/factor_engineering/technical.py`（指标库）+ `config/diagnostics.yaml`（规则卡, 冻结）+ `config/factor_registry.yaml`（diagnostic_indicators 段）+ `src/strategies/diagnosis.py`（计分/解释）+ `src/orchestration/diagnosis_service.py`（统一入口/状态机/快照）。
> 证据：新增 66 项测试（technical 29 / diagnosis 23 / diagnosis_service 14）→ 全量 **584/584**；真实数据副本端到端冒烟（`rehearsal_m3.duckdb`，见 M3-06）。
> **修正记录（M3-05）**：`price_version` 由 `源|口径|最后日期` 改为 `源|口径`——原含日期会把每个新交易日误判为"版本变化"；诊断日期由快照 `as_of` 承载（唯一键含 as_of，不受影响）。

- [x] **P11-M3-01 指标函数库**（2026-09-29 完成）
  - 产出：`technical.py` — MA/偏离/斜率（SMA）、12-1 精确端点区间动量、ATR/ATR%（复用 `RiskManager.calc_atr_series`）、20 日对数收益波动率（ddof=1）+ 历史经验分位、RSI（Wilder，零涨/零跌/双零=50）、布林带（SMA20±2σ 总体标准差；带宽 0 → 位置空值）、量额活跃度与 ATR% 相对基准（基准 shift(1)）；`compute_indicators(params)` 组合入口 + `indicator_columns(params)` 动态列名（窗口不硬编码）。
  - 证据：`tests/test_technical.py` 29 例手工期望值（SMA/偏离/斜率/动量端点不拆收益/ATR% 手算/波动率/分位/RSI 递推/布林 ddof=0/零带宽/活跃度基准/零波无除零/短历史全空/参数缺失报错）。
  - 验收：通过 — P11-A3 核心；无 MACD/KDJ/OBV 等未要求指标。

- [x] **P11-M3-02 诊断规则卡与指标注册（已冻结）**（2026-09-29 完成）
  - 产出：`config/diagnostics.yaml`（rules_version=**diag-v0.1-research**, 冻结于 2026-09-28；计分项全为必需（缺失→总分为空）；组上限 trend 30 / momentum 10 / volatility 25；组合上限 trend+momentum=35；归一化 `score=min(raw,60)/60×100`；分档 <30 正常 / 30–60 警戒 / ≥60 防御；升级需连续 2 运行日、恢复 1 日；展示项与提示阈值；事件敏感列清单）；`factor_registry.yaml` 新增 `diagnostic_indicators:`（D01–D12，含公式/口径/单位/窗口/最低样本/缺失策略/用途/代码入口）。
  - 证据：`tests/test_diagnosis.py`（配置加载/必填校验/上限一致性/分档序/列名存在性）+ 注册表一致性测试（全条目可导入可调用）。
  - 验收：通过 — 全部数值标注"研究示例"（未宣称用户确认/已验证）；先冻结后评估；修改需新版本号。

- [x] **P11-M3-03 计分引擎**（2026-09-29 完成）
  - 产出：`strategies/diagnosis.py::assess_row` — 分项原值→触发→贡献→组上限→组合上限（比例折减保持项级可追溯）→归一化→原始三态；缺必需→总分为空不出三态；缺失去向记录（不视为低风险）；展示项不参与总分。
  - 证据：`tests/test_diagnosis.py` 手工期望值（满分场景折减 ×0.875 组贡献 26.25/8.75、中间值 32.5→54.2、分档边界 30.0/60.0、负风险归零、缺项为空、组上限不重复计票）。
  - 验收：通过 — P11-A4 核心。

- [x] **P11-M3-04 解释模板（无 LLM）**（2026-09-29 完成）
  - 产出：`_item_explanation`（tmpl-v0.1：当前值→比较对象/触发规则→结论→贡献→数据日期/来源）+ 组合折减/结论行 + 提示文案（超买/超卖/带外/放量/缩量均为"关注"语义, 非方向判断）；模板版本随规则卡 `explanation_version` 记录。
  - 证据：解释结构测试（每项含当前值/规则/贡献/日期+来源；缺项为空说明）+ 冒烟示例（600519 解释行实测可读）。
  - 验收：通过 — 无 LLM 调用；文案数字与指标一致。

- [x] **P11-M3-05 诊断快照与状态变化记录**（2026-09-29 完成）
  - 产出：`diagnosis_service` 快照落库 — 确定性 ID `symbol|as_of|rules_version|price_version`；状态机 `resolve_status_transition`：首次"初始"（不虚构升级）、升级"待确认"→连续 N 日确认后"新增警戒/新增防御"、回落"恢复"、同档"维持"、规则/口径变化"版本变化"、数据无效"数据失效"、必需指标缺失"不可评估"；同日重跑与更早运行日比较（不与自己对话）；`run_kind` 标记 live/rebuild；payload/history/列表读取接口。
  - 证据：`tests/test_diagnosis_service.py` 状态机 9 例 + 端到端 4 例（正常→待确认→新增防御路径、同日重跑幂等、事件暂停→不可评估、取数失败→数据失效、短历史→不可评估）。
  - 验收：通过 — P11-A9 核心；重复运行不新增行。

- [x] **P11-M3-06 统一诊断服务**（2026-09-29 完成）
  - 产出：`run_diagnosis(db, symbol, refresh, ...)` 单入口 — 身份→取数（新鲜度/受控刷新）→权益事件（暂停/调整）→指标（必要时用已核验调整序列）→计分/解释→状态机→快照→UI 结构；`get_snapshot_payload/history`、`snapshot_display_frame` 供三入口读取（同版本同输入同结果）。
  - 证据：真实数据副本端到端（`rehearsal_m3.duckdb`）：600519 → 更新 6014 行、评分 **37.7 警戒**（trend 17.16/momentum 5.45/vol 0）、5/5 完整；510300 → **23.9 正常**（含 RSI 29.9 超卖提示/布林下轨/缩量提示）；513100 → 事件暂停 **score=None、不可评估**；同日重跑 snapshots 3→3 幂等；交易表零变化（accounts/proposals/strategy_states 0→0）。
  - 验收：通过 — P11-A2 服务层部分。

### P11-M4 — 条件基本面 / ETF 产品检查 ✅ 完成 2026-09-29

> 通则：每子项按 M1-07 结论"可用→实现；部分可用→实现并标限制；暂缓→登记不实现"。检查结果状态：**未触发/关注/重点复核/无法评估** + 不适用原因；与技术分数**并列展示**，不混入技术分、不让技术好分抵消产品问题；UI 区分数据问题与市场/财务问题。
> 交付：`src/data_pipeline/fundamental_data.py`（取数规范化）+ `config/condition_rules.yaml`（研究示例 cond-v0.1-research, 冻结）+ `src/strategies/condition_checks.py`（规则评估）+ 服务/UI 接入（快照 `checks.conditions` + 详情页"条件检查"表格）。
> 证据：新增 49 项测试（解析 18 / 规则 27 / 服务 3 / securities 兜底 1 净增）→ 全量 **662/662**；真实数据副本冒烟（`rehearsal_m4.duckdb`）见 M4-01/03。
> **接口加固**：实测深交所站点 (`www.szse.cn`) 超时导致股票名录不可用 → 新增**新浪全量行情快照兜底**（`stock_zh_a_spot`，含北交所）；名录加载失败统一归一为 `SecurityFetchError`（不再泄漏原始超时堆栈）。同时修复 pandas NaN 泄漏（`parse_float` 对 NaN/Inf → None）。

- [x] **P11-M4-01 股票财务检查（盈利/现金流/偿债）**（2026-09-29 完成）
  - 产出：三大报表 + 财务指标（扣非）规范化（按报告期合并; 公告日期/币种; 字符串 'None'/NaN → None）→ 9 项检查：营收同比（基期非正/缺失 → 无法评估, 不输出误导增长率）、归母净利（当期亏损 → 重点复核; 扣非为负 → 关注提示）、TTM（单季合成, 缺期不硬凑）、经营现金流质量（OCF 为负/占净利过低 → 关注）、现金流多期（近 4 单季）、资产负债率（70%/85%）、短期偿债覆盖（货币资金/短期借款+一年内到期, 近似口径声明; 字段缺失 → 无法评估）、归母净资产（为负 → 重点复核）、**金融企业按报表结构识别 → 偿债项不适用**。
  - 证据：`tests/test_condition_checks.py`（同比阈值/负基数/负利润/扣非/单季与 TTM/OCF/负债率/覆盖率/负净资产/金融不适用）+ `tests/test_fundamental_data.py`（报表合并/短债求和/扣非合并/银行结构识别/估值分位）。
  - 真实冒烟（600519）：营收 922.78 亿 +1.3%、归母 445.17 亿 -2.0%、TTM 814.34 亿/扣非 813.67 亿、OCF/净利 1.59、负债率 15.2%、短期有息负债 0.57 亿（覆盖 941 倍）→ 全部未触发；各项含报告期与公告日期。
  - 验收：通过 — P11-A6（数据问题与财务问题分离: 缺失→无法评估; 亏损/高负债/弱现金→关注/重点复核）。

- [x] **P11-M4-02 股票估值检查（PE-TTM/PB）**（2026-09-29 完成）
  - 产出：`stock_value_em` 日频史取最新 PE-TTM/PB + 自身历史分位（窗口 1250/最少 250 样本, 参数来自规则卡）；**PE≤0（亏损）/PB≤0（负净资产）→ 无法评估, 明确"不解释为便宜"**；分位 ≥90% → 关注（研究示例）；样本不足 → 仅展示数值。
  - 证据：规则测试（负 PE/高分位/样本不足）+ 真实冒烟：600519 PE 18.97（分位 2%）、PB 6.15（分位 3%）→ 未触发。
  - 验收：通过 — P11-A6 估值部分；不使用统一 PE 阈值跨行业。

- [x] **P11-M4-03 ETF 产品检查**（2026-09-29 完成）
  - 产出：8 项 — 身份/跟踪标的/成立/管理人（`fund_overview_em`）；规模（<5 亿关注/<2 亿重点复核 + 季度口径与超期提示）；费率（管理+托管 >0.60%/年 → 关注）；成交条件（近 20 日日均成交额 <3000 万关注/<500 万重点复核, 与既有流动性硬线一致）；买卖价差（`fund_etf_category_sina` 快照, 时点数据声明, 日线不伪造价差）；折溢价（`fund_etf_fund_daily_em` 净值+市价校验: **当日净值缺失/不匹配 → 无法评估并注明"不解除既有禁买"**；|折溢价|>3% → 关注）；**跟踪质量与底层估值 → 不适用/暂缓（原因显式展示）**；黄金/债券底层估值明确不适用 PE。
  - 证据：规则测试（规模/费率/流动性/价差/折溢价/暂缓项/商品不适用）+ 全表快照进程内 TTL 缓存（1h, 不与"每次打开都全表抓取"冲突）。
  - 真实冒烟：510300 → 跟踪沪深300、规模 948.72 亿（2026-06-30）、费率 0.20%、日均成交 31.72 亿、折溢价 +0.05% → 全部未触发；**513100（跨境）→ 费率 0.80% 关注、当日净值缺失 → 折溢价无法评估（不解除禁买）**。
  - 验收：通过 — P11-A7。

- [x] **P11-M4-04 条件子项状态固化**（2026-09-29 完成）
  - 产出：本节与附录 A 同步"已实现/暂缓"；暂缓项（跟踪质量、底层估值映射）在 UI 条件检查表内以"不适用 + 暂缓原因"显式展示（不隐藏、不补零）；附录 A"M1 结论"列同步更新实施状态。
  - 验收：通过 — 不以"基本面完整"概括部分支持。

### P11-M5 — 三入口、观察名单与六个月 K 线 ✅ 完成 2026-09-29

> 交付：`dashboard/diagnostics_data.py`（只读数据层）+ `dashboard/diagnostics_app.py`（诊断模块渲染）+ `dashboard/app.py` 双模块入口 + `orchestration.diagnosis_service` 名单服务包装；Plotly 6.9.0 入主依赖（D1）。
> 证据：新增 27 项测试（数据层 16 / UI-AppTest 7（含详情页快照渲染）/ 名单服务 4）→ 全量 **611/611**；UI 数据链路冒烟（`rehearsal_m5.duckdb`）：观察名单增/幂等/移除、510300 评分 23.9 正常、513100 不可评估、列表装配排序/变化/完整度、K 线全史 1879 → 显示 126 行（首行 MA200 已预热）；headless 启动 health=ok/index=200。

- [x] **P11-M5-01 观察名单服务（受控写入口）**（2026-09-29 完成）
  - 产出：`diagnosis_service.watchlist_add/remove/update_note` — 先确认身份再快照身份字段；增/删幂等（软删除保留记录与变更历史）；不写策略池。
  - 证据：`TestWatchlistService` 4 例（身份快照/幂等去重/软删除与变更记录/名单外标的拒绝）+ 冒烟（active=[510300], removed=[600519]）。
  - 验收：通过 — 加入不改变策略可交易池（instruments/universe_members 零写入有测试）。

- [x] **P11-M5-02 「我的持仓」入口**（2026-09-29 完成）
  - 产出：`diagnostics_data.load_holdings` — 账户事件回放（复用 AccountService，只读）+ 最近可用收盘价估值与实际权重；真实/模拟模式标签；无账户/无事件 → "未配置/未导入"提示（**不生成伪持仓**）；出池持仓仍可诊断（价格读取池内优先）。
  - 证据：数据层测试（无账户/未知账户/手工核对权重 total）；AppTest 断言真实库未配置账户时显示提示。
  - 验收：通过 — P11-A2 持仓部分。
  - 注：持有期 ATR 止损展示依赖已有账户-止损状态链路（`strategy_states`）；当前库无账户故无持仓期状态，出池/止损展示不做伪造（行为已由数据层保证）。

- [x] **P11-M5-03 「探索」入口**（2026-09-29 完成）
  - 产出：`_render_explore` — 表单提交式查询（非逐键请求）；6 位代码直查/名称模糊搜索→候选选择；错误代码与未找到均明确提示（不猜测）；结果**不自动**加入观察名单（显式按钮）；加入动作经受控写入口。
  - 证据：`_resolve_candidates` 逻辑 + AppTest（未提交不触发身份解析）；错误路径由 securities 层测试覆盖（UnknownSymbolError）。
  - 验收：通过 — P11-A1。

- [x] **P11-M5-04 诊断详情页与六个月日 K 线**（2026-09-29 完成）
  - 产出：详情排列（评分卡→问题清单→K 线→指标/贡献→解释）；Plotly 蜡烛图 + 成交量/成交额副图、MA20/60/200 开关、布林带按需叠加、周末隐藏、悬停/缩放；**全历史计算 MA/布林后再裁剪近六个月**（显示与计算分离）；口径/数据范围/短历史说明随图展示；权益事件仅在核验路径下调整（未核验 → 问题清单说明）。
  - 证据：`TestKlineFrame`（六个月窗口/首行 MA20 用窗口前历史/MA200 补足后口径一致/零带宽不崩）+ `TestKlineFigure`（开关与副图切换）+ 冒烟 `build_kline_frame` 全史 1879→126 行。
  - 验收：通过 — P11-A5 核心；OHLC 与均线同口径（同帧计算）。

- [x] **P11-M5-05 刷新、缓存与并发**（2026-09-29 完成）
  - 产出：打开详情先读快照 → 缺失/落后于"最近已完成交易日"时受控自动更新（会话内冷却 300s，防重绘/切页重复抓取）；「手动刷新」走 `refresh="force"`；失败保留旧结果并提示原因（返工入口）；休市（接口无新增）与接口失败在服务层区分（stale/failed/notes）；写冲突由 DuckDB 锁显式报错并在 UI 呈现。
  - 证据：`_auto_run_if_needed` 冷却与新鲜度逻辑 + `_run_and_handle` 错误兜底（代码与 AppTest 覆盖渲染路径）；失败保留旧数据由 M2 服务层测试覆盖（`test_fetch_failure_keeps_cache`）。
  - 验收：通过 — P11-A8 核心行为。

- [x] **P11-M5-06 列表视图与顶部状态**（2026-09-29 完成）
  - 产出：`build_list_rows` — 名称/代码/来源/（持仓）实际权重/分数/状态/较上次变化/主要原因/完整度/数据日期；排序 防御>警戒>不可评估>正常>未评估，同档按分数降序；主要原因优先级（缺项 > 提示 > 状态）；详情页顶部展示规则/口径/数据截止/运行类型与备注。
  - 证据：`TestListRows`（排序/变化文案/原因优先级/权重格式/空表）+ 冒烟（513100 不可评估排在 510300 正常之前——数据问题优先可见）。
  - 验收：通过 — P11-A8 展示部分。
  - 注：状态筛选/列排序由 Streamlit 表格原生支持（`dataframe` 交互）；筛选项（checkbox 过滤）未单列，按需后续补充。

- [x] **P11-M5-07 既有账户页面回归**（2026-09-29 完成）
  - 产出：`app.py` 重构为双模块（侧边栏"账户与信号 / 单标的诊断"，缺省为账户与信号）；既有账户视图拆为 `render_account_panel(settings, db_path)`，内容不变；诊断模块懒加载（不用时不引入 plotly）。
  - 证据：既有 `tests/test_dashboard.py` 22 项全绿（含 AppTest 无头执行与账户区块断言）；新增 AppTest 断言切回账户模块后 实际 vs 目标/净值对照 仍在。
  - 验收：通过 — 既有功能与只读连接不受影响。

- [ ] **P11-M5-08（可选）外部诊断推送**
  - 状态：**本轮未实施（登记）** — 默认关闭不阻塞（plan-phase1.1 §6）；接线点：`run_diagnosis` 的 trigger 变化 + 既有 `notifier` 通道（需用户明确启用与授权，测试用模拟通道）。

### P11-M6 — 回归、验收与交接 ✅ 完成 2026-09-29

- [x] **P11-M6-01 P11-A1–A10 验收对照**（2026-09-29 完成）
  - 产出：§11.1 对照表（每项引用测试名/命令/运行记录；无受阻项）。
  - 验收：通过。

- [x] **P11-M6-02 必要回归**（2026-09-29 完成）
  - 证据（§11.6）：全量 **663/663**；`rehearsal_m7.py` 在 v8 库上 ALL_OK（迁移/回滚/重跑/恢复）；`uv run backtest` 偏差 0.043%/0.031% + T+0/T+1 交收差一天；真实库交易类事实前后零变化。
  - 验收：通过。

- [x] **P11-M6-03 在线端到端演示**（2026-09-29 完成）
  - 产出：§11.2 六类样本在线演示（完整 ETF/跨境 ETF/短历史股票/普通股票/金融企业股票/错误代码），证据 `data/backups/m6_e2e_demo_20260929_2246.json`；观察名单加入→再次打开可见；再次打开详情读快照不重取；含冷启动（szse 超时→sina 兜底≈20s）与耗时记录。
  - 验收：通过。（真实库已留 2 条观察名单与 5 份演示快照，即产品设计行为。）

- [x] **P11-M6-04 交接报告**（2026-09-29 完成）
  - 产出：§11.3（P11-D1–D9）§11.4（支持矩阵）§11.5（命令/版本）§11.7（**P1-R1–R6 独立状态 + 研究局限**）§11.8（AGENTS 评估：无需修订）。
  - 验收：通过 — 未以"页面可用"勾掉 Phase 1 研究任务（P1 全部保持未开始并列出）。

- [x] **P11-M6-05 文档同步**（2026-09-29 完成）
  - 产出：README（命令与测试数）、`docs/user_guide.md`（单标的诊断 + 条件检查使用说明）、`docs/PROJECT_MEMORY.md`（EV-028…EV-034 + 索引 + 起点）、`HANDOFF.md`（重写为 Phase 1.1 收官交接）；AGENTS 评估见 §11.8（无需修订）。
  - 验收：通过。

---

## 5. 关键设计决策与待决事项

### 5.1 已做出的实现级决策（记录理由，普通技术选择）

| # | 决策 | 理由 | 状态 |
| :--- | :--- | :--- | :--- |
| D1 | 图表 **Plotly**（K 线蜡烛图）：`uv add plotly`（已装 6.9.0，入主依赖；p1 组同步移除避免重复声明）；altair 保留为备选 | spec §2 展示选型为 Streamlit + Plotly；K 线蜡烛图与悬停/缩放依赖其原生能力。plotly 原在 p1 组声明，属依赖组调整而非新第三方依赖；不启用整个 p1 extra（避免 alphalens 在 Py3.14 的不确定性, I-02） | ✅ 已实施（M5） |
| D2 | 非池标的行情单独存 `diag_price_daily`，不复用 `ohlcv_daily` | `ohlcv_daily` 是 19 只策略池标的的真理源；混入股票/非池 ETF 会污染全局日期范围、质检统计与既有不变量测试。诊断行情是可重建缓存 | ✅ 已实施（M2） |
| D3 | 证券身份缓存独立建 `securities` 表，不扩展 `instruments` | `instruments` 承载池语义（premium_check_required、池同步派生）；股票不适用该语义 | ✅ 已实施（M2） |
| D4 | 诊断指标注册到 `config/factor_registry.yaml` 新增 `diagnostic_indicators:` 段；规则卡（权重/分档/时效）独立 `config/diagnostics.yaml` | 满足 AGENTS §4 注册要求（factor_engineering 新增指标函数必须入库），同时与 Phase 1 收益预测因子的 `factors:` 段分离，避免污染 P1 因子清单与未来 CI | ✅ 已实施（M3；D01–D12 + 一致性测试） |
| D5 | 诊断快照存 JSON 列（metrics/contributions/checks），与既有 `runs.detail_json` 风格一致 | 轻量、避免表爆炸；查询场景为按 symbol 取最新/历史 | ✅ 已实施（M2） |
| D6 | 刷新走受控同步更新（首版），复用 validator 质检（含权益事件感知），critical 阻断不绕过 | plan-phase1.1 P11-M5 允许受控同步；既有 I-09 纪律（不得用绕过方式写脏数据）优先 | ✅ 已实施（M2 取数层；M5 接线） |
| D7 | 未核验权益事件 → 跨事件指标暂停/不可评估 + 原因；已核验 → 按声明调整 | spec §8.4.3-5；当前 12 条全部未核验，实现以合成样本测试覆盖两条路径；真实 513100 → paused 验证 | ✅ 已实施（M2） |
| D8 | 诊断全链路不写账户/目标/建议/池；`tests` 用表计数前后不变证明 | spec §8.4.7；保证 Phase 0.5 既有功能不受影响 | ✅ 已实施（M2；M3 快照层继续覆盖） |

### 5.2 待决事项（M1 后状态）

| # | 事项 | 处理（2026-09-28 更新） |
| :--- | :--- | :--- |
| Q1 | 名称→代码搜索是否可用 | ✅ **可用**：`stock_info_a_code_name`（5569 股票）+ `fund_etf_category_sina`（1687 ETF）；`fund_name_em` 辅助。同名歧义按候选列表处理（M2-01） |
| Q2 | NAV/折溢价是否可用 | ⚠️ **部分可用**：`fund_etf_fund_daily_em` 折价率快照 + `fund_open_fund_info_em` 净值历史；**跨境当日净值可能缺失**（513100 实测 `---`）→ 日期/币种不匹配一律 unknown（**不解除**既有禁买） |
| Q3 | 财务披露日是否可得 | ✅ **可得**：Sina 报表含"公告日期"字段 → 展示披露期；历史 PIT 研究需版本化快照（本阶段不做回测注入，登记限制） |
| Q4 | 评分规则卡的最终数值 | M3-02 冻结前为草案（附录 B）；冻结后作为 `rules_version` 记录，不宣称已验证 |
| Q5 | 外部诊断推送是否本轮启用 | 默认否（P11-M5-08 可选） |

---

## 6. Phase 1 研究任务独立登记（不由本阶段验收）

> 依据 spec §8.3（P1-R1–R6）、plan §8。本阶段**不勾选、不验收**以下任务；仅登记其与本阶段的衔接关系。Phase 1.1 完成后仍需单独推进。

| 编号 | 事项 | 与本阶段关系 | 当前状态 |
| :--- | :--- | :--- | :--- |
| P1-R1 | 权益/数据前置及 A 路径回测一致性（含 I-09 方案2：权益事件入账） | 本阶段复用"已核验/未核验"事件区分与价格暂停规则；**完整入账不在本阶段** | 未开始（真实持仓前必须完成） |
| P1-R2 | 少量候选因子 + 定义注册（第一轮优先一个明确动量定义） | 本阶段精确 12-1 实现可被 P1 复用；**不替代** P1 的因子注册与截面评估 | 未开始 |
| P1-R3 | RankIC/ICIR 及小截面分组诊断 | 无依赖；本阶段不做 IC 评估 | 未开始 |
| P1-R4 | 简单权重、B0/B1/A 比较及少量模块消融 | 无依赖；本阶段不比较策略优劣 | 未开始 |
| P1-R5 | 时间划分、参数邻域、失败披露与成本压力报告 | 无依赖 | 未开始 |
| P1-R6 | 可比案例下的独立工程对照（vectorbt/等价） | I-02 版本决策归 P1；本阶段不迁移环境 | 未开始 |

另登记（非 P11 交付）：`scripts/validate_registry.py` 缺失（pyproject `factor-check` 悬空引用）→ 归 P1-R2 注册检查一并建设；本阶段仅新增 `diagnostic_indicators` 定义与轻量一致性测试。

---

## 7. schema v8 迁移（✅ 已实施 2026-09-28，P11-M2-02）

> 只新增表，不改既有表列；迁移按 plan §5：备份 → 副本演练（幂等 + counts 核对）→ 真实库应用 → 留痕。
> 实施证据：`scripts/rehearsal_v8.py` + `data/backups/rehearsal_v8_report.json`（副本 23 表 counts 稳定、真实库 versions [1..8] 且 counts 稳定）；备份 `quant_pilot_20260928_pre_v8.duckdb`。

```sql
-- 1) 证券身份缓存 (诊断用, 与池语义无关)
securities (
  symbol VARCHAR PRIMARY KEY,        -- 6 位代码
  name VARCHAR, exchange VARCHAR,    -- SH/SZ/BJ
  sec_type VARCHAR,                  -- stock / etf
  market_type VARCHAR,               -- A股/跨境/商品… (ETF 沿用; 股票=A股)
  adjustment VARCHAR,                -- 研究价格口径: none/qfq/hfq (每标的单一口径)
  source VARCHAR, fetched_at TIMESTAMP, note VARCHAR
)

-- 2) 观察名单 (加入不加入策略池)
watchlist (
  symbol VARCHAR PRIMARY KEY, name VARCHAR, exchange VARCHAR, sec_type VARCHAR,
  added_at TIMESTAMP, note VARCHAR, status VARCHAR DEFAULT 'active',  -- active/removed
  updated_at TIMESTAMP
)

-- 3) 名单变更记录 (增/删/备注)
watchlist_changes (
  id BIGINT, symbol VARCHAR, action VARCHAR, -- add/remove/note_update
  detail VARCHAR, changed_at TIMESTAMP
)

-- 4) 非池标的研究行情缓存 (可重建; 池内 ETF 仍读 ohlcv_daily)
diag_price_daily (
  symbol VARCHAR, date DATE,
  open/high/low/close DOUBLE, volume DOUBLE, amount DOUBLE,
  source VARCHAR, adjustment VARCHAR, fetched_at TIMESTAMP,
  PRIMARY KEY (symbol, date)
)

-- 5) 诊断快照 (只保存实际运行过的诊断日)
diagnostic_snapshots (
  snapshot_id VARCHAR PRIMARY KEY,   -- 确定性: symbol+as_of+rules+price 哈希
  symbol VARCHAR, as_of DATE, calculated_at TIMESTAMP,
  rules_version VARCHAR, price_version VARCHAR,
  run_kind VARCHAR DEFAULT 'live',   -- live / rebuild
  metrics_json VARCHAR,              -- 原始指标
  contributions_json VARCHAR,        -- 分项贡献
  checks_json VARCHAR,               -- 条件检查项与缺失原因
  score DOUBLE, status VARCHAR,      -- 正常/警戒/防御 (缺失=null)
  completeness VARCHAR,              -- 数据完整度
  trigger VARCHAR,                   -- 初始/新增警戒/新增防御/恢复/数据失效/版本变化
  UNIQUE (symbol, as_of, rules_version, price_version)
)
```

## 8. 验收对照（P11-A1–A10 → 任务映射）

| 验收 | 场景与期望（spec §8.4.8） | 覆盖任务 |
| :--- | :--- | :--- |
| P11-A1 | 身份/歧义/错误码；查询与名单不改变账户/策略池 | M1-02、M2-01、M2-06、M5-03 |
| P11-A2 | 三入口同版本一致；无账户不生成伪持仓；出池持仓可查看 | M2-06、M3-06、M5-02 |
| P11-A3 | 手工样本核对 MA/动量/ATR/RSI/布林/量额；零波/短历史不除零 | M3-01 |
| P11-A4 | 分组上限/分档/缺失/贡献可核对；相似指标不重复计票 | M3-02、M3-03、M3-04 |
| P11-A5 | 六个月显示+更长计算；OHLC 与均线同口径；拆分/停牌/缺失可说明 | M2-04、M2-05、M5-04 |
| P11-A6 | 财报期/披露日/TTM/同比/单位；负 PE；不适用行业 | M1-04、M1-05、M4-01、M4-02 |
| P11-A7 | ETF 净值缺失/过期/估值不适用/不可比降级；不解除禁买 | M1-06、M4-03 |
| P11-A8 | 重复交互/新交易日/首查/失败/休市/并发行为明确 | M2-03、M5-05、M5-06 |
| P11-A9 | 快照版本/修订/变化可追溯；无 LLM/无订单/无副作用 | M2-06、M3-05 |
| P11-A10 | 逐项可用/部分/暂缓+证据；核心演示；P1 独立追踪 | M1-07、M6-01、M6-03、M6-04 |

---

## 9. 附录 A：数据接口能力调查表（P11-M1 实测 ✅，2026-09-28）

> 纪律：每行均有真实调用证据（`data/backups/probe_phase11_20260928_231728.json` / `_232009.json` / `_232343.json`，探针 `scripts/probe_phase11_interfaces.py`）；未实测的行如实标注，不折算成"可用"。

| 检查项 | 必要字段 | 接口/来源（首选） | 代表标的/样本 | 样本·历史深度 | 披露/统计期 | 单位·口径 | 缺失/错误行为 | 当前查询 | 历史研究 | 结论 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| ETF 日线 | OHLCV/量额 | `fund_etf_hist_sina` | 510300 / 513100 | 3486 / 3200 行；2012-05→2026-09-28 | 日频（盘后） | 元/股/元；**不复权**（额外列丢弃） | 错误代码→空表 | ✅ | ✅（不复权；跳变按暂停规则） | **可用（首选）** |
| 股票日线 | OHLCV/量额 | `stock_zh_a_daily`（sina） | 600519 raw+qfq / 688981（2020 上市短历史） | 6013 / 1500 行；2001-08→2026-09-28 | 日频 | 元；含 outstanding_share/turnover；`adjust=''`/`qfq` 均可得 | 错误代码→`KeyError('date')` | ✅ | ✅（qfq="当前复权"，修订需重建） | **可用（首选）** |
| 股票日线（备用） | 同上+振幅/换手 | `stock_zh_a_hist`（东财） | 600036 | 178 行（小段测试） | 日频 | 中文列名 | 偶发 RemoteDisconnected（1/2 次失败）；末根滞后至 09-24 | ⚠️ 偶发 | ⚠️ 同左 | 部分可用（备用源，带重试） |
| 股票身份/名称 | code+name | `stock_info_a_code_name` | 全表 | 5569 行；含北交所 920xxx | 快照（9.5s） | — | 整表失败→error | ✅ | —（快照缓存） | **可用** |
| ETF 身份/名称 | 代码/名称/买卖价/成交额 | `fund_etf_category_sina` | 全表 | 1687 行；sh/sz 前缀 | 快照 | 元 | — | ✅ | —（快照缓存） | **可用** |
| 基金名录（辅助） | 代码/简称/类型 | `fund_name_em` | 全表 | 27991 行（全基金，需过滤） | 快照 | — | 体量较大 | ✅ | — | 可用（辅助） |
| 单只股票资料 | 行业/上市等 | `stock_individual_info_em` | 600519 | ❌ RemoteDisconnected | — | — | 东财偶发断链 | ⚠️ | — | 部分（不作唯一依赖） |
| 交易日历 | 交易日列表 | `tool_trade_date_hist_sina` | 全表 | 8797 行；1990-12-19→**2026-12-31**（含未来） | — | 日期 | — | ✅ | ✅ | **可用** |
| 股票盈利 | 营收/归母/扣非 | `stock_financial_report_sina`（利润表） | 600519 / 601398（金融） | 103 / 90 行；1998→2026-06-30 | 报告期 + **公告日期** + 是否审计 | 元；字符串，缺失=`'None'` 文本 | 字段大面积为 None | ✅ | ✅（公告日期≈披露日；PIT 需快照版本化） | **可用** |
| 股票现金流 | 经营现金流等 | 同上（现金流量表） | 600519 | 99 行；2000→2026-06-30 | 同上 | 同上 | 同上 | ✅ | ✅（同上） | **可用** |
| 股票偿债 | 净资产/负债等 | 资产负债表 + `stock_financial_analysis_indicator` | 600519 | 103 行 / 指标 86 列（`start_year` 控制） | 季/年 | 元/百分比 | 同上 | ✅ | ✅（同上） | **可用** |
| 股票估值 | PE-TTM/PB/市值 | `stock_value_em`（首选）/ `stock_zh_valuation_baidu`（备用） | 600519 | 2121 行（2018 起）/ 366 行（近一年） | 日频 | 倍/元 | 负值真实存在（负 PE/PEG）；`stock_a_indicator_lg` **本版本不存在** | ✅ | ✅（首选）/部分（备用窗口短） | **可用** |
| ETF 净值历史 | 单位/累计净值 | `fund_open_fund_info_em`（单位净值走势） | 510300 | 3502 行；2012-05→2026-09-28 | 日频 | 元 | — | ✅ | ✅ | **可用** |
| ETF 净值+折溢价快照 | NAV/市价/折价率 | `fund_etf_fund_daily_em` | 全表 1669 行（抽检 513100/159920/518880） | 当日+前一估值日两列 | 日频 | 元/%；`---` 为缺失 | **跨境当日净值可能缺失**（513100 实测 `---`）→ 折价率同缺 | ✅ 部分 | —（快照） | **可用（须日期匹配校验）** |
| ETF 资料/费率/规模 | 跟踪标的/费率/规模 | `fund_overview_em` | 510300 / 513100 | 1 行 20 列 | 规模截止 2026-06-30（季度） | %/年、亿元 | — | ✅ | 季度 | **可用** |
| ETF 规模份额 | 份额 | `fund_etf_scale_szse` / `fund_etf_scale_sse` | 全表 / 沪市 | 1060 行 / 593 行（2025-01-15）；**近期日期失败** | 定期 | 份 | SSE 近期日期不可得 | ✅ / ⚠️ 部分 | 部分 | 深市可用；沪市部分（运行时探测最近可用期） |
| ETF 快照（备用） | 净值/申赎状态 | `fund_etf_spot_ths` | 全表 | 1728 行 | 快照 | 元 | 部分 None | ✅ | — | 可用（替代东财） |
| ETF 分红（登记） | 日期/累计分红 | `fund_etf_dividend_sina` | 510300 | 14 行；2012→2026-01-19 | 事件 | 元 | — | ✅ | ✅ | 可用（仅登记；入账属 P1-R1） |
| 故障接口登记 | — | `fund_etf_fund_info_em` ❌ValueError；`fund_etf_spot_em` ❌ProxyError；`fund_etf_hist_em` qfq ❌ConnectionError(1次)；`fund_individual_*_xq` ❌KeyError；`fund_fee_em` 空表 | — | — | — | — | 均有替代或已登记 | ✖/⚠️ | ✖ | 暂缓（不阻塞，替代见上） |

**失败情形记录（调用→返回→判定）**：
1. `bad_code_stock_sz999999` → `KeyError('date')`（归一化前抛出）→ 错误代码必须由服务层捕获并提示"代码不存在或不可查询"。
2. `bad_code_etf_sh999999` → 空表 → 需与"新上市/无数据"区分（先用身份接口确认身份，再判断数据）。
3. `stock_individual_info_em`（600519）→ RemoteDisconnected → 东财偶发；不得依赖单次成功（退避重试 + 备用）。
4. `fund_etf_fund_info_em`（510300/513100）→ ValueError 长度不匹配 → 本版本接口损坏；改用 `fund_open_fund_info_em` / `fund_etf_fund_daily_em`。
5. `fund_etf_spot_em` → ProxyError（push2delay.eastmoney.com）→ 本代理环境失败；用 THS/新浪替代。
6. `fund_etf_hist_em`（qfq）→ ConnectionError（同批 `stock_zh_a_hist` 成功）→ 东财偶发；备用 + 重试。
7. `stock_a_indicator_lg` → AttributeError（函数不存在）→ 改用 `stock_value_em`。
8. `fund_etf_scale_sse(date='20260925')` → KeyError（返回结构不含预期列）→ 近期日期不可得，需运行时探测最近可用统计期（默认日期可用）。
9. `fund_individual_basic_info_xq` / `fund_individual_detail_info_xq` → `KeyError('data')` → 雪球接口本环境不可用；不依赖。
10. `fund_fee_em` → 空表 → ETF 交易费率口径不适用；费率以 `fund_overview_em`（管理费/托管费）为准。

**M1 结论与降级决策（→ M2/M4/M5 输入）**：
- **进入实现（可用）**：核心行情（ETF=新浪、股票=新浪 qfq 为主/EM 备用）；身份（股票全表 + ETF 列表 + 名录辅助）；交易日历；财务三大报表 + 指标；估值（`stock_value_em` 主 + 百度备用）；ETF 资料/费率/规模（`fund_overview_em` + 深市份额；沪市份额部分）；净值历史 + 折溢价快照（**日期/币种匹配校验，不匹配一律 unknown**）。
- **部分可用 + 降级**：EM 备用路径（重试/退避，不作唯一依赖）；SSE 份额（运行时探测最近可用期，不可得展示"暂无"）；折溢价（跨境滞后 → unknown，不解除既有禁买）；`stock_individual_info_em`（不作依赖）。
- **暂缓（明确记录，不隐藏、不阻塞核心交付）**：ETF 跟踪质量（缺可比全收益指数收益，不以价格指数/高相关替代）；ETF 底层估值映射（缺可靠 ETF→指数映射；黄金/债券不适用）；雪球资料接口（本环境失败）。
- **M4 已承诺范围**：股票 盈利/现金流/偿债/估值；ETF 身份资料/费率/规模/成交条件/净值与折溢价。**暂缓**：跟踪质量、底层估值。
- **实施状态（P11-M4 完成, 2026-09-29, 全量 662/662）**：已承诺子项全部实现（`fundamental_data.py` + `condition_rules.yaml`(cond-v0.1-research) + `condition_checks.py` + 服务/UI 接入）；暂缓项（跟踪质量、底层估值映射）在 UI 以"不适用 + 暂缓原因"显式展示。接口加固：深交所站点超时 → 新浪全量快照兜底股票名录（实测生效）；pandas NaN 泄漏已修复（`parse_float` NaN/Inf → None）。

---

## 10. 附录 B：诊断规则卡（✅ 已冻结实施 2026-09-29，P11-M3-02）

> 权威来源：`config/diagnostics.yaml`（rules_version=**diag-v0.1-research**, 冻结于 2026-09-28）；
> 指标实现与口径：`src/factor_engineering/technical.py` 模块 docstring；
> 注册条目：`config/factor_registry.yaml` → `diagnostic_indicators:`（D01–D12）。

**冻结值摘要**（研究示例；修改需产生新 rules_version）：
- 指标参数：MA 20/60/200；斜率窗口 20；12-1 动量 lookback=252/skip=21（端点映射显式）；ATR 14（Wilder）；波动率 20 日；波动分位窗口 252（min 120）；RSI 14；布林 20/2σ（总体标准差）；活跃度 60 日（基准 shift(1), min 20）；ATR% 相对基准 60 日（min 20）。
- 计分（全部为必需项；缺一 → 总分为空、不出三态）：
  - 趋势组（上限 30）：ma200_dev（深度 10% = 20 分）+ ma200_slope（-3% = 10 分）；
  - 动量组（上限 10）：momentum_12_1（-20% = 10 分）；
  - 组合上限：trend+momentum 合计 **35**（比例折减，项级可追溯）；
  - 波动组（上限 25）：vol_pctile（0.5 起计, 1.0 = 15 分）+ atr_pct_ratio（+50% = 10 分）；
  - 归一化：`score = min(raw, 60) / 60 × 100`；
  - 分档：<30 正常；30–60 警戒；≥60 防御；升级需连续 2 个运行日、恢复 1 日确认（版本变化单独触发，不冒充市场变化）。
- 展示项（不参与总分，无方向判断）：RSI14（70/30 提示）、布林位置/带宽、量额比（2.0/0.5 提示）、ATR%、MA、动量。
- 缺失策略：必需项缺失 → 总分为空 + 原因；展示项缺失不重分配权重；数据完整度（n/5）随结果展示。
- 数据时效：'最近已完成交易日'为正式诊断时点（本地 cutoff 18:30）；stale/failed 数据照常给出结果并附注。

---

## 11. Phase 1.1 验收与交接报告（P11-M6, 2026-09-29）

### 11.1 P11-A1–A10 验收对照（spec §8.4.8）

| 验收 | 期望 | 结果 | 证据（测试/命令/运行记录） |
| :--- | :--- | :--- | :--- |
| P11-A1 | 身份/歧义/错误码；查询与名单不改变账户/策略池 | ✅ | `test_securities.py`（UnknownSymbolError/名录兜底/前缀推断不猜测）；`TestWatchlistService`（身份快照/幂等/不写 instruments·universe_members）；在线演示：`999999` → 明确报错（0.0s）且不生成脏记录 |
| P11-A2 | 三入口同版本一致；无账户不生成伪持仓；出池持仓可查看 | ✅ | 单入口 `run_diagnosis` 三页复用（M5）；`load_holdings` 无账户→"未配置"（数据层测试+AppTest）；`test_out_of_pool_etf_with_ohlcv_history_is_pool`（出池仍可诊断） |
| P11-A3 | 手工样本核对 MA/动量/ATR/RSI/布林/量额；零波/短历史不除零 | ✅ | `test_technical.py` 29 例手工期望值；在线短期样本：301696（118 根）→ 不可评估 1/5，不伪造 |
| P11-A4 | 分组上限/分档/缺失/贡献可核对；相似指标不重复计票 | ✅ | `test_diagnosis.py`（组合折减 ×0.875、边界 30/60、缺项为空、展示项不计分）；规则卡一致性测试（`test_diagnostics_config` 等价校验） |
| P11-A5 | 六个月显示+更长计算；OHLC 与均线同口径；拆分/停牌/缺失可说明 | ✅ | `test_diagnostics_ui_data.py`（全史预热/首行 MA20 用窗口前历史/零带宽）；冒烟：K 线 1879→126 行；513100 事件暂停说明 |
| P11-A6 | 财报期/披露日/TTM/同比/单位；负 PE；不适用行业不误报 | ✅ | `test_condition_checks.py`（负基数/负利润/扣非/TTM/金融不适用/负 PE"不解释为便宜"）；在线：600519（TTM 814 亿/公告日期齐全）、601398（银行偿债不适用 + PE 分位 98% → 关注） |
| P11-A7 | ETF 净值缺失/过期/估值不适用/不可比降级；不解除禁买 | ✅ | PR-01 unknown 测试（含"不解除既有禁买"文案）；在线 513100 演示；跟踪质量/底层估值以"不适用+暂缓原因"展示 |
| P11-A8 | 重复交互/新交易日/首查/失败/休市/并发行为明确 | ✅ | 自动更新冷却+手动刷新+失败保留（M5）；`test_fetch_failure_keeps_cache`、`test_no_new_rows_keeps_stale_note`；单写冲突显式报错说明（M2-03） |
| P11-A9 | 快照版本/修订/变化可追溯；无 LLM/无订单/无副作用 | ✅ | 状态机 9 例（初始/待确认/确认/恢复/版本变化）；副作用测试（存储层+服务层+演示 before==after）；解释为模板（无 LLM 调用） |
| P11-A10 | 逐项可用/部分/暂缓+证据；核心演示；P1 独立追踪 | ✅ | 附录 A 实施状态（M4-04）+ 本节 §11.2–11.7；P1-R1–R6 独立保留（§6/§11.7） |

### 11.2 在线端到端演示（P11-M6-03；真实库，2026-09-29）

证据：`data/backups/m6_e2e_demo_20260929_2246.json`（含每标的耗时与逐项状态）。

| 样本 | 类型 | 首跑耗时 | 数据 | 技术分/状态 | 条件检查 |
| :--- | :--- | ---: | :--- | :--- | :--- |
| 510300 沪深300ETF | 完整数据 ETF | 8.2s / 4.7s* | 1879 行→09-29 | 23.9 正常（5/5） | 未触发 6 · 不适用 2 |
| 513100 纳指ETF国泰 | 跨境 ETF | 0.5s / 0.3s* | 1878 行→09-29 | 不可评估（事件暂停 0/5） | 关注 1（费率 0.80%）· **折溢价无法评估（不解除禁买）** |
| 301696 三瑞智能 | 短历史股票（118 根） | 45.7s† / 1.1s* | 118 行→09-29 | 不可评估（1/5，缺 MA200/动量） | 未触发 10（已披露报表有限但口径正确） |
| 600519 贵州茅台 | 普通股票 | 4.8s / 4.3s* | 6014 行→09-29 | 37.7 警戒（5/5） | 未触发 10（TTM 814 亿/负债率 15.2%/PE 分位 2%） |
| 601398 工商银行 | 金融企业股票 | 4.5s / 3.7s* | 4822 行→09-29 | 0.0 正常（5/5） | 关注 2（PE 分位 98%/PB 95%）· 偿债不适用 · 净资产 4.33 万亿 |
| 999999 | 错误代码 | 0.0s | — | — | `UnknownSymbolError`（明确提示，不猜测） |

\* 第二次（热缓存）运行耗时；† 含名录冷启动（深交所站点超时 → 新浪全量快照兜底 ≈20s，随后写入 `securities` 缓存）。
**流程验证**：加入观察名单（510300/601398）→ 重新打开列表可见已保存分数/状态（"首次"，5/5）；再次打开详情直接读取快照（无重取）。**交易表零变化**（accounts/account_events/targets/proposals/feedback/strategy_states/universe_members before==after）。

### 11.3 P11-D1–D9 交付结果

| 需求 | 结果 |
| :--- | :--- |
| P11-D1 能力核对与接口报告 | ✅ 附录 A（36 项实测 + 失败情形）；M4 追加名录兜底（szse 超时→sina） |
| P11-D2 三入口 | ✅ 我的持仓/观察名单/探索（同入口双模块） |
| P11-D3 指标、评分与规则 | ✅ diag-v0.1-research 冻结规则卡 + 计分/三态/解释（无 LLM） |
| P11-D4 六个月日K线 | ✅ 全史预热裁剪显示 + MA/Boll 开关 + 量额切换 + 口径标注 |
| P11-D5 股票基本面（条件） | ✅ 已实测并按能力表交付（盈利/现金流/偿债/估值） |
| P11-D6 ETF 产品检查（条件） | ✅ 资料/费率/规模/成交/价差/折溢价；**跟踪质量、底层估值 → 暂缓（显式展示）** |
| P11-D7 按需更新/刷新/快照 | ✅ 新鲜度优先 + 冷却去抖 + 手动刷新 + 快照历史/状态变化 |
| P11-D8 统一展示与异常变化记录 | ✅ 列表排序（防御>警戒>不可评估>正常）+ 变化/原因/完整度；外发推送未实施（登记，默认关闭） |
| P11-D9 验收与交接 | ✅ 本节 + §11.5 运行说明 + §11.7 P1 独立状态 |

### 11.4 支持范围矩阵

- **已支持**：境内 A 股（含北交所）与境内 ETF（含跨境）的代码/名称查询、诊断详情、观察名单、快照历史；技术指标/评分/三态；条件检查（上述已交付子项）。
- **部分支持**：东财备用路径偶发（重试+新浪主力）；沪市 ETF 份额（仅历史期）；估值分位（需 ≥250 样本，窗口 ~5 年）；名录冷启动需网络（sina 兜底，首次约 20s）。
- **暂缓（不隐藏）**：ETF 跟踪质量（缺可比全收益指数）；ETF 底层估值映射；诊断外发推送。
- **不适用**：黄金/债券类底层估值（PE/PB 不适用）；金融企业普通工商偿债门槛。
- **明确排除**（spec §8.4.8）：LLM、公告解析、全市场扫描、股票交易建议、假设买后组合、穿透/相关性分析、复杂归因、ML。

### 11.5 运行命令与版本

```powershell
uv run streamlit run dashboard/app.py     # 入口: 侧边栏切换"账户与信号 / 单标的诊断"
# 环境变量 QUANT_PILOT_DB=<path> 可指向演练副本（默认 settings.storage.db_path）
uv run pytest tests/ -q                   # 全量 663 项
uv run python scripts/rehearsal_m7.py     # Phase 0.5 隔离演练 (ALL_OK=True)
uv run python scripts/rehearsal_v8.py --apply   # schema v8 演练/应用 (已完成)
uv run python scripts/probe_phase11_interfaces.py  # 接口探针 (可选复跑)
```

版本：schema **v8**；技术规则 **diag-v0.1-research**（config/diagnostics.yaml, 冻结 2026-09-28）；条件规则 **cond-v0.1-research**（config/condition_rules.yaml, 冻结 2026-09-29）；解释模板 **tmpl-v0.1**；数据口径 ETF=不复权 / 股票=后复权（页面标注）。

### 11.6 必要回归证据（P11-M6-02）

- 全量测试 **663/663 通过**（2026-09-29；M0–M6 净增 216 项，无失败无跳过）。
- `scripts/rehearsal_m7.py`：迁移 versions [1..8]、counts 不变、半笔事件回滚、重跑去重、恢复核对 → **ALL_OK=True**（Phase 0.5 机制在 v8 库上保持）。
- `uv run backtest`：510300 偏差 **0.043%**、513100 偏差 **0.031%**（验收 <1%）；T+0/T+1 交收相差 1 个交易日 ✓。
- 真实库事实保持：账户/事件 0→0（演示前后）；既有信号/权益事件/实验记录未被诊断链改写。

### 11.7 P1-R1–R6 独立状态（**未被本阶段注销**）

| 编号 | 状态 | 说明 |
| :--- | :--- | :--- |
| P1-R1 | 未开始 | 权益事件核验与入账（I-09 方案2，真实持仓前必须完成）；A 回测风控对齐 |
| P1-R2/R3 | 未开始 | 少量因子注册 + RankIC/ICIR 评估（本阶段 12-1 实现可复用，但独立验收） |
| P1-R4/R5 | 未开始 | B0/B1/A 对照与模块消融、时间划分与成本压力报告 |
| P1-R6 | 未开始 | vectorbt/等价独立对照（I-02 版本决策仍在 P1） |

**仍待研究的效果问题**（本阶段已显式标注"研究规则"）：技术评分与条件阈值未经有效性验证；风险提示的波动/下行走势及误报/漏报研究、动量预测评估均属 P1/后续。**阶段完成不等于实盘授权**（真实账户启用 Gate 未触发）。

### 11.8 AGENTS 修订评估

本阶段新增文件均落于既有目录规范（spec §5 / AGENTS §6）；`factor_registry.yaml` 新增 `diagnostic_indicators:` 与 `factors:` 分离（用途=diagnostic，不参与 IC）。**无需修订 AGENTS.md**；Phase 1 建设 `scripts/validate_registry.py` 时可将该段纳入校验。

---

## 12. 变更记录

| 日期 | 记录 |
| :--- | :--- |
| 2026-09-26 | 初始版本（P11-M0-01/M0-03 完成）：仓库实测基线（447/447、schema v7、19 ETFs）；§1 差异核对；§4 任务清单（M0–M6）；§5 设计决策 D1–D8 与待决 Q1–Q5；§6 Phase 1 研究任务独立登记；§7 schema v8 草案；§8 验收映射；附录 A/B 模板。**等待用户确认（P11-M0-04）后进入 P11-M1。** |
| 2026-09-26 | **P11-M0 完成**（用户确认）：P11-M0-02 修正 task-phase0.5.md 两处状态残留（M2 标题 / 入金样本行，证据引用测试名）；P11-M0-04 用户确认任务清单。下一步：P11-M1 接口实测。 |
| 2026-09-28 | **P11-M1 完成**（36 项真实调用，3 份探针 JSON）：核心行情（ETF/股票=新浪可用；东财备用偶发）、身份（股票 5569 + ETF 1687 可用）、日历（可用）、财务三大报表+指标（可用，含公告日期）、估值（`stock_value_em` 主+百度备用）、ETF 资料/费率/规模/净值/折溢价（可用或部分可用）。暂缓：跟踪质量、底层估值映射（缺可比数据，明确记录）。附录 A 逐行填实；M4 范围据此裁剪；Q1–Q3 关闭。 |
| 2026-09-28 | **P11-M2 完成**：schema v8 五表（securities/watchlist/watchlist_changes/diag_price_daily/diagnostic_snapshots）+ CRUD；`securities.py` 身份解析（名录缓存/搜索/前缀推断）；`diag_data.py` 统一取数（池内/非池、新鲜度优先、增量 UPSERT + 股票 hfq 全量替换、超时/重试、失败保留旧数据、validator 质检门）+ 交易日历 + 权益事件检查（paused/data_issue/调整）。股票口径 qfq→hfq（理由见 M2-03）。新增 71 测试 → **518/518**；`rehearsal_v8` 副本演练 + 真实库应用（counts 稳定）；真实副本冒烟（600519 updated 6013 行；513100 paused、ohlcv 增量至 09-28；交易表零变化）。 |
| 2026-09-29 | **P11-M3 完成**：指标库 `technical.py`（MA/12-1/ATR%/波动率分位/RSI/布林/量额，29 项手工期望值测试）；规则卡 `config/diagnostics.yaml` **冻结**（diag-v0.1-research：组上限 30/10/25、组合上限 35、归一化 min(raw,60)/60×100、分档 30/60、升级 2 日确认）+ `factor_registry.yaml` 注册段 D01–D12；计分引擎与解释模板（tmpl-v0.1，无 LLM）；快照状态机（初始/待确认/新增警戒/新增防御/恢复/维持/版本变化/数据失效/不可评估）+ 统一服务 `diagnosis_service.py`。**修正**：`price_version` 去掉数据日期（口径=源|复权；日期由 as_of 承载），避免常规更新误判版本变化。新增 66 测试 → **584/584**；真实副本端到端（600519 警戒 37.7/510300 正常 23.9 含提示/513100 事件暂停不可评估；同日重跑幂等；交易表零变化）。 |
| 2026-09-29 | **P11-M5 完成**：Plotly 6.9.0 入主依赖（D1）；`dashboard/diagnostics_data.py`（只读数据层：六个月 K 线全史预热/列表装配/持仓回放）+ `dashboard/diagnostics_app.py`（我的持仓/观察名单/探索 → 详情：评分卡/问题清单/K线/指标/解释；自动更新冷却去抖 + 手动刷新）+ `app.py` 双模块入口（账户与信号保持默认；`QUANT_PILOT_DB` 可覆盖库路径供演练）+ `diagnosis_service` 名单服务包装（幂等/软删除/不写策略池）；user_guide/README 增"单标的诊断"使用说明。新增 27 测试 → **611/611**（含临时库快照驱动的详情页 AppTest）；UI 数据链路冒烟（观察名单增删/评分 23.9 正常与不可评估排序/K线 1879→126 行首行 MA200 已预热）；headless 启动 health=ok。P11-M5-08 外发推送本轮未实施（登记，默认关闭）。 |
| 2026-09-29 | **P11-M4 完成**：`fundamental_data.py`（股票三大报表+指标+估值规范化；ETF 资料/净值折价/买卖价快照解析；全表快照 1h TTL 缓存）+ `config/condition_rules.yaml`（cond-v0.1-research 冻结）+ `condition_checks.py`（股票 9 项 + ETF 8 项；未触发/关注/重点复核/无法评估/不适用；金融企业不适用、负基数不给增长率、亏损 PE 不给"便宜"、缺净值折溢价 unknown 且不解除禁买）+ 服务（`checks.conditions` 入快照, 失败不阻塞技术诊断）与 UI（详情页"条件检查"表格 + 暂缓项显式展示）。**接口加固**：深交所站点超时 → 新浪全量快照兜底股票名录；名录加载失败归一 `SecurityFetchError`；pandas NaN 泄漏修复（`parse_float` NaN/Inf→None, 实测 SV-02 从"nan 亿"修正为 0.57 亿）。新增 51 测试 → **662/662**；真实冒烟：600519 十项全未触发（TTM 814 亿/负债率 15.2%/PE 分位 2%）、510300 六项未触发（费率 0.20%/折溢价 +0.05%）、513100 费率 0.80% 关注 + 折溢价无法评估（跨境净值未更新）。 |
| 2026-09-29 | **P11-M6 完成 → Phase 1.1 全部批次收官**：验收报告 §11（P11-A1–A10 对照全过/支持矩阵/P11-D1–D9/P1-R1–R6 独立状态/AGENTS 评估无需修订）；在线端到端演示（真实库 6 类样本; 证据 `m6_e2e_demo_20260929_2246.json`; 冷启动 szse 超时→sina 兜底）；回归 **663/663** + `rehearsal_m7` ALL_OK（v8 库）+ `backtest` 偏差 0.043%/0.031%；修复银行权益字段名（"归属于母公司股东的权益"回退, 601398 SV-03 恢复"未触发"）并刷新真实快照；观察名单空态测试改为隔离库（不再依赖真实库可变状态）。文档同步: README/user_guide/PROJECT_MEMORY(EV-034)/HANDOFF。 |
