# PROJECT_MEMORY.md — Quant-Pilot 项目持久记忆

> **定位**: 项目的"外脑"。记录实际发生的事实、新增约束、设计变更。与 `task.md`（原始计划，只读）互补。
>
> **更新规则**: 演化记录只追加不删除。每次 Phase 完成或关键决策做出后必须更新。详细更新指令见 `prompts/update_memory_prompt.md`。

---

## 一、项目状态快照

| 字段                   | 值                                                                  |
| :--------------------- | :------------------------------------------------------------------ |
| **项目启动日期** | 2025-07-15                                                          |
| **当前 Phase**   | Phase 0.5 + M8 ✅ → **Phase 1.1 ✅ 完成（P11-M0–M6, 2026-09-29）** —— 单标的诊断页面可用；下一步 **Phase 1 研究（P1-R1–R6，未开始）** |
| **当前 Task**    | `docs/task-phase1.1.md`（P11-M0–M6 全 ✅；§11 验收与交接报告）；Phase 1 研究任务独立登记于其 §6（P1-R1–R6, 全部未开始） |
| **Phase 进度**   | Phase 0 代码 T0.1~T0.10 ✅ / §8.1 收尾补项 6 组 ✅（A0-1…A0-6 全部闭合；推送失败持久化转 M2） |
| **Gate 状态**    | 工程 Gate + P05-D1–D8 ✅（审计 §5）; **真实账户启用 Gate 未触发**（需用户明确启用, spec §8.7） |
| **最后更新**     | 2026-09-29 (Phase 1.1 P11-M6 完成 → 阶段收官: 验收对照+在线演示+回归 663/663; 交接见 HANDOFF.md) |

---

## 二、Phase 执行快照

### Phase 0 — MVP 闭环 (✅ 已完成)

- **状态**: ✅ P0 完成 (2026-09-13) — T0.1~T0.10 全部交付, Gate 6/6 通过
- **开始日期**: 2025-07-18
- **预计完成**: 启动后 6-8 周
- **实际交付物**:
  - `pyproject.toml` — 项目配置 (P0 依赖) + CLI 入口修复
  - `config/settings.yaml` — 全局参数 + 19 ETF 标的池 (inline YAML) + Sina 数据源
  - `src/data_pipeline/fetchers/akshare_fetcher.py` — 双源获取 (Sina/eastmoney)
  - `src/data_pipeline/duckdb_manager.py` — DuckDB 6 表 + 幂等 UPSERT + 信号读写 (upsert_signals/get_latest_signals)
  - `src/data_pipeline/processors/validator.py` — 5 项数据质量护栏
  - `src/strategies/macro_timing.py` — 宏观择时 (维度①②) + 仓位映射
  - `src/strategies/universe_filter.py` — 标的清洗 (4 项筛选 + 流动性警告)
  - `src/strategies/portfolio_optimizer.py` — 波动率倒数加权 + 迭代裁剪
  - `src/strategies/risk_manager.py` — ATR 移动止损 + 宏观骤降减仓
  - `src/orchestration/daily_pipeline.py` — 每日盘后主流程 (9 步串联五层漏斗) 🆕
  - `src/orchestration/notifier.py` — 消息推送 (PushPlus, 四要素归因, 重试 3 次, CLI 集成) 🆕
  - `src/backtest/custom_loop.py` — 自研回测引擎 (T+0/T+1 撮合, 摩擦成本, 动态仓位上限, 整手) 🆕
  - `dashboard/data_loader.py` — 面板数据层 (信号/持仓视图/价格曲线/宏观历史, 可单测) 🆕
  - `dashboard/app.py` — Streamlit 信号面板 (宏观卡/信号表/持仓/走势/宏观趋势) 🆕
  - `data/quant_pilot.duckdb` — 19 ETFs 全量历史 OHLCV (34,073 rows) + 11 条真实信号
  - `tests/test_duckdb_manager.py` — 14 tests ✅
  - `tests/test_validator.py` — 21 tests ✅
  - `tests/test_macro_timing.py` — 20 tests ✅
  - `tests/test_universe_filter.py` — 39 tests ✅
  - `tests/test_portfolio_optimizer.py` — 43 tests ✅
  - `tests/test_risk_manager.py` — 41 tests ✅
  - `tests/test_daily_pipeline.py` — 6 tests ✅ 🆕 (含幂等/脏数据阻断/无未来函数)
  - `tests/test_notifier.py` — 22 tests ✅ 🆕 (PushPlus mock HTTP, 重试/token 边界)
  - `tests/test_custom_loop.py` — 20 tests ✅ 🆕 (撮合时序/成本/交收/真实数据验收)
  - `tests/test_dashboard.py` — 16 tests ✅ 🆕 (含 AppTest 冒烟/真实库一致性)
- **实际完成**: 2026-09-13 (全量测试 242/242, 交付物 10/10)
- **P0 遗留承接 (P1 须关注)**:
  - I-02: Python 3.14 vs vectorbt/alphalens 兼容性 (P1 启动前决策)
  - EV-006: NAV/IOPV 未入库, 溢价检查静默跳过
  - EV-008: 复权口径 — 待修订 AGENTS §5.2 条款 + Sina 数据 DIY 后复权
  - EV-011: 组合优化 Top-N 截断致仓位利用率低 (P1 风险平价时评估)
- **Gate Criteria 状态**:
  - [x] 闭环验收：daily_pipeline.py 完整跑通 → 2026-09-11 全流程 EXIT=0, PushPlus 真实推送成功
  - [x] 回测正确性：510300 买入持有年化 vs 自身价格基准偏差 0.044% < 1% (2026-09-13, T0.9)
  - [x] T+0/T+1 正确性：510300 (T+1) 与 513100 (T+0) 交收日相差 1 个交易日 (2026-09-13, T0.9)
  - [x] 数据护栏有效：脏数据阻断测试 (test_negative_price_blocks_pipeline)
  - [x] 无未来函数：test_future_data_does_not_change_past_result
  - [x] 信号可追溯：推送含标的/方向/目标仓位/触发原因四要素 (2026-09-11 真实推送验证)
- **Phase 0.5 新增口径说明**: 上述 6/6 Gate 为**旧口径**；spec v3.0 §8.1 新口径下 Phase 0 收尾尚缺 A0-1…A0-6 证据（策略版本/数据时间/回测手工对账/分红限制/批次可卖/面板模拟标注），见 `docs/phase0.5-audit.md` §1。

### Phase 0.5 — 既有实现纠偏、账户与基准 ([~] 进行中)

- **状态**: M0 初审 ✅ (2026-09-14) → M1 待用户审阅后启动
- **M0 产出**: `docs/phase0.5-audit.md` (C01–C23 逐项 + §8.1 剩余项)、`docs/task-phase0.5.md` (A0 补项 + M1–M7 任务)
- **下一步**: 用户审阅 → M1 (A 类纠偏 + Phase 0 收尾) → M2…M7 逐批实施，每批验证后停靠汇报

---

## 三、演化记录 (Evolution Log)

### EV-001 — P0 标的池从 ~50 缩至 20 只 ETF

- **日期**: 2025-07-18
- **类型**: 范围变更
- **与 task.md 关系**: 互补 (task.md 写 ≥50, 实际先做 20)
- **描述**: 用户要求初始标的池从 ~50 只缩减到 ~20 只 (6 宽基 + 7 行业 + 6 跨境/商品), 跑通后再扩充。更轻量的 MVP 交付。
- **影响范围**: `config/settings.yaml` universe 字段、后续所有回测/策略模块的标的范围
- **后续动作**: P0 闭环后, 如需扩展标的池, 直接在 settings.yaml 追加并重跑 fetch-data

### EV-002 — Python 3.14 与 P1/P2 依赖兼容性问题

- **日期**: 2025-07-18
- **类型**: 技术债务
- **与 task.md 关系**: 互补
- **描述**: 当前环境 Python 3.14。vectorbt 依赖 numba→llvmlite, 后者不支持 Python >3.12。vectorbt 和 alphalens-reloaded 从默认依赖中移除, 放入 `[project.optional-dependencies]` 的 p1/p2 分组。P1 时需解决 Python 版本问题 (降级或使用 --python 3.12)。
- **影响范围**: P1 回测引擎验证、P2 ML 模型训练
- **后续动作**: P1 启动时评估 `uv python pin 3.12` 或使用 Docker/WSL

### EV-003 — hatchling flat layout 导入路径约束

- **日期**: 2025-07-18
- **类型**: 新增约束
- **与 task.md 关系**: 互补
- **描述**: hatchling 以 `packages = ["src/data_pipeline", ...]` 安装时, 各子包暴露为顶层模块 (如 `import data_pipeline` 而非 `from src.data_pipeline import ...`)。所有项目内导入必须使用 `from data_pipeline.xxx import ...` 格式。
- **影响范围**: 所有 `.py` 文件中的项目内 import 语句
- **后续动作**: 新增模块时遵循此规则; CI/lint 不应报 `src.` 导入为错误

### EV-004 — eastmoney API 反封锁与代理绕过

- **日期**: 2025-07-18
- **类型**: 发现依赖 / 新增约束
- **与 task.md 关系**: 互补
- **描述**: eastmoney (push2his.eastmoney.com) 对高频请求会临时封 IP (表现为 RemoteDisconnected 或 ProxyError)。已实现三重防护: ①每次 API 调用间随机休眠 0.5~3s; ②失败指数退避重试 (5s→10s→20s); ③`requests.Session(trust_env=False)` 绕过 Windows 不可达系统代理。测试中曾成功拉取 131 rows (510300), 但连续密集测试后触发了 IP 暂时限流。
- **影响范围**: 所有依赖 Akshare 的数据获取操作; CI 中不应频繁调用真实 API
- **后续动作**: ①生产运行 `uv run fetch-data` 时单次执行, 不会频繁重试导致封 IP; ②后续单元测试使用合成数据或 mock, 不依赖真实网络; ③若长期不稳定, 考虑引入 JQData 作为备用数据源 (spec.md §4.1 已有规划)

### EV-005 — 反封锁休眠参数记录

- **日期**: 2025-07-18
- **类型**: 新增约束
- **与 task.md 关系**: 互补
- **描述**: 用户明确要求 "每次获取数据后加上一个随机休眠 0.5~3 秒, 模拟人类行为, 防止被封"。已实现在 `AkshareFetcher._random_sleep()`, 参数从 `config/settings.yaml` 的 `data_fetch.min_sleep` / `max_sleep` 读取。
- **影响范围**: 数据获取速度 (20 ETFs × ~1.75s 平均休眠 ≈ 35s 纯等待时间)
- **后续动作**: 若需调整休眠范围, 修改 settings.yaml 即可, 无需改代码

### EV-007 — 数据源从 eastmoney 迁移至 Sina (新浪财经) 🔴

- **日期**: 2025-07-19
- **类型**: 重大架构变更
- **与 task.md 关系**: 互补 (T0.1 数据获取方案升级)
- **描述**: eastmoney (push2his.eastmoney.com) 对 IP 限流过于激进, 0.5-3s 随机休眠仍在前 3 只 ETF 后被封, 无法完成全量数据导入 (19 ETFs 需 ~33,000 rows)。调研后切换至 akshare 的 Sina 财经接口 `ak.fund_etf_hist_sina`:
  - **零封禁**: 19 ETFs 连续拉取无一失败
  - **速度快**: 0.1-0.5s/只 (vs eastmoney 0.5-3s 休眠 + 高失败率)
  - **历史深**: 510050 追溯到 2005 年 (5201 rows)
  - **代价**: Sina 返回不复权数据 (ETF 分红影响小, 年化 1-2%); eastmoney 保留为备用源 (后复权 hfq)
  - **Symbol 格式**: Sina 需要交易所前缀 — 51xxxx → sh51xxxx, 15xxxx → sz159915
  - **全量导入**: 33,313 rows 成功写入 DuckDB
- **影响范围**: `akshare_fetcher.py` 重构为双源架构; `settings.yaml` 新增 `data_fetch.source: "sina"`; eastmoney 路径改 `qfq`→`hfq`
- **后续动作**: P1 通过 `fund_etf_dividend_sina` 获取分红数据, DIY 计算后复权价格

### EV-008 — 前复权→后复权修正 🟡

- **日期**: 2025-07-19
- **类型**: 设计修正
- **与 task.md 关系**: 互补
- **描述**: CLAUDE.md §5.2 规定"所有技术指标基于前复权价格计算", 但量化研究标准应使用**后复权 (hfq)**。后复权保留最早绝对价格为真实交易价, 后续价格按分红因子等比放大, 价格序列始终为正。前复权会导致历史价格失真 (甚至变负)。事实核查: 两者对数收益率 `ln(P_t/P_{t-1})` 一致, 但均线等绝对价格指标在后复权下更可靠。eastmoney 备用路径已改为 `adjust="hfq"`; Sina 主力路径暂为不复权 (参见 EV-007)
- **影响范围**: `akshare_fetcher.py` eastmoney 路径; CLAUDE.md §5.2 规则 #3 需修正 (下次 CLAUDE.md 修订时)
- **后续动作**: 下次 CLAUDE.md 大版本更新时将"前复权"改为"后复权"; P1 实现 Sina 数据的 DIY 后复权

### EV-009 — daily_pipeline 信号落库与无状态设计 (T0.7)

- **日期**: 2025-07-24
- **类型**: 关键设计决策
- **与 task.md 关系**: 互补 (T0.7 实现细节)
- **描述**: T0.7 每日主流程的三个核心设计决策:
  - **前日宏观得分无状态化**: 不建宏观得分历史表, 而是将 OHLCV 截断最新一日后重算 `MacroTiming.evaluate` 得到 prev_score, 供骤降检测. 保证幂等且无状态漂移
  - **信号幂等**: `signal_id = 交易日 YYYYMMDD × 1000 + 当日序号` 确定性生成, `DuckDBManager.upsert_signals` 以 ON CONFLICT 更新, 同日重跑无重复行; `get_latest_signals(before_date=run_date)` 取昨日目标权重做 BUY/SELL/HOLD 对比, 同日信号不参与对比
  - **风控二次优化**: 止损剔除或宏观骤降触发时, 用幸存标的 + 有效仓位上限重跑组合优化, 并保留原始 forced_exits 名单供信号归因
  - **调仓防抖**: `risk_management.turnover_threshold` (15%) 应用于信号层 — 目标权重变动 < 阈值时维持昨日目标 (HOLD), 避免无效调仓
- **影响范围**: `src/orchestration/daily_pipeline.py`, `src/data_pipeline/duckdb_manager.py` (新增 upsert_signals/get_latest_signals); T0.8 notifier 应直接消费 `PipelineResult.summary()` 与 `trade_signals.trigger_reason`; T0.9 回测循环应复用同一信号语义
- **后续动作**: T0.8 从 `trade_signals` 表读取最新信号推送; 若未来持仓数 > 1000/日需调整 SIGNAL_ID_DATE_FACTOR

### EV-010 — Windows GBK 控制台 emoji 崩溃 🟡

- **日期**: 2025-07-24
- **类型**: 新增约束
- **与 task.md 关系**: 互补
- **描述**: `uv run daily` 全流程跑通后在 `print(result.summary())` 处抛 `UnicodeEncodeError: 'gbk' codec can't encode character '✅'` — risk_manager.py 的风控摘要含 emoji (✅/⚠️/🛑), Windows GBK 控制台无法编码. 修复: `main()` 中 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`. 注意: logging (stderr) 不受影响但 GBK 终端显示为乱码, 属已知显示问题
- **影响范围**: 所有 CLI 入口 (`daily`, 未来 `backtest`/notifier 调试脚本); 新增 CLI 若 print 含 emoji 的中文摘要必须做同样处理
- **后续动作**: T0.8 notifier 推送走 HTTP 不受控制台编码影响; 若日后要彻底消除终端乱码, 可在 logging 配置中统一 `errors="replace"` 的 StreamHandler

### EV-011 — 组合优化 Top-N 截断导致总权重低于仓位上限 (观察)

- **日期**: 2025-07-24
- **类型**: 发现依赖 / 技术债务
- **与 task.md 关系**: 互补
- **描述**: 真实冒烟运行发现: 19 只标的通过清洗后, 逆波动率权重 × cap(27.5%) 再经 max_positions=5 截断, 总权重仅 16.6% (< 27.5%) — 截断发生在归一化之后, 被截掉标的的权重直接丢失而非再分配. 这是 T0.5 的既有行为 (43 tests 通过, 属设计内), 但在大标的池 + 低 cap 场景下仓位利用率明显偏低
- **影响范围**: `src/strategies/portfolio_optimizer.py::_apply_caps`; P0 信号偏保守 (少建仓, 方向正确)
- **后续动作**: P1 升级风险平价 (T1.7) 时评估"先截断 Top-N 再归一化"或"截断后再分配剩余权重"; P0 不改动已测试行为

### EV-006 — P0 NAV/IOPV 数据尚未入库

- **日期**: 2025-07-18
- **类型**: 发现依赖
- **与 task.md 关系**: 互补
- **描述**: T0.4 标的清洗模块的溢价检查需要 NAV/IOPV 数据, 但 T0.1 仅拉取了 OHLCV 日线数据 (akshare `fund_etf_hist_em` 可提供 `unit_nav` 字段但未使用)。当前溢价检查设计为: 若提供 NAV 数据则执行溢价过滤, 否则静默跳过。不影响其他 3 项筛选 (成交量/上市天数/集中度) 的正常运行。
- **影响范围**: T0.7 daily_pipeline 串联时, 若需要完整的溢价检查, 需在数据拉取步骤中增加 NAV 数据获取
- **后续动作**: P0 后期评估是否需要为溢价检查扩展数据拉取; 或延至 P1 因子计算时一并解决

### EV-012 — T0.8 消息推送模块 (PushPlus) 与 token 安全策略

- **日期**: 2026-09-11
- **类型**: 关键设计决策 / 新增约束
- **与 task.md 关系**: 互补 (T0.8 实现细节)
- **描述**: T0.8 notifier 的核心决策与验收事实:
  - **消息四要素**: `build_message()` 纯函数生成标题+正文, 每条信号含标的 (代码+名称)/方向/目标仓位/触发原因; 非 HOLD 信号独立分节, HOLD 折叠至 "维持原仓" 节; 全 HOLD 时标题标 "无操作" 但仍推送 (每日存活确认)
  - **重试语义**: 与 fetcher `max_retries` 一致 — 总尝试次数 = `notification.retry_count` (3 次含首次), 指数退避 1s/2s; 网络异常与 PushPlus 业务码 ≠ 200 统一重试
  - **推送失败不回滚**: `push_pipeline_result` 永不抛异常, 返回 `PushResult(ok/attempts/error)`; daily CLI 只告警, 不改变退出码 (信号已落库, 推送是尽力而为)
  - **token 安全**: `config/settings.yaml` 被 git 跟踪, 真实 token 不落盘 — 验收用 `PUSHPLUS_TOKEN` 环境变量注入; 解析优先级 settings > 环境变量
  - **代理绕过**: PushPlus 请求 `trust_env=False`, 绕过 Windows 不可达系统代理 (沿用 EV-004 教训)
  - **验收**: 2026-09-11 `PUSHPLUS_TOKEN=xxx uv run daily` 真实推送, PushPlus 返回 code=200 (尝试 1/3); 全量测试 206/206 通过
- **影响范围**: `src/orchestration/notifier.py`, `tests/test_notifier.py` (22 tests), `daily_pipeline.py::main` 增 `_push_signals()`; P0 Gate "闭环验收" "信号可追溯" 由本条闭环
- **后续动作**: T0.10 dashboard 可参考推送格式; 若需 token 持久化, 需先与用户确认 (settings.yaml 加 .gitignore 或本地凭证文件)

### EV-013 — 自研回测循环 v0.1 设计决策与成本口径确认 (T0.9)

- **日期**: 2026-09-13
- **类型**: 关键设计决策 / settings 参数变更
- **与 task.md 关系**: 互补 (T0.9 实现细节)
- **描述**: T0.9 回测引擎的核心决策与验收事实:
  - **成本口径 (用户拍板)**: 原三方不一致 — settings `commission_bps: 0.15` + 注释"万分之1.5" + CLAUDE §5.2"双边佣金 ≥0.15%" — 用户确认采用 **round-trip 0.15% = 单边 0.075%**; 滑点维持宪法公式 max(0.15%/笔, 金额/20日均额×0.5%); settings trading_cost 段重构为无歧义字段 (commission_rate=0.00075 等), 新增 backtest.initial_cash=100000
  - **撮合时序**: 信号 T 日收盘生成 → T+1 开盘价成交 (禁 T 日收盘价); 卖出先于买入 (卖出资金当日可用); 整手 100 份; 现金不足自动缩减 (不超买, cash ≥ 0)
  - **T+0/T+1 语义 (用户确认)**: 买入份额按 settlement_type 解锁 (T+0 当日 / T+1 次日), 成交记录携 settlement_date; 锁定期卖单顺延至解锁日开盘执行
  - **防未来函数细节**: 滑点用的 20 日均成交额以 shift(1) 计算, 仅含 T-1 及之前数据
  - **验收基准 (用户确认)**: "与指数实际走势偏差 <1%" 采用 ETF 自身价格序列为基准 (纯验证引擎记账正确性, 不含跟踪误差); 同口径对比: 信号次日开盘买入 → 期末收盘
  - **验收结果**: 510300 买入持有 2019-01-02 → 2026-09-11, 年化 5.75% vs 基准 5.80%, **偏差 0.044% < 1%** ✓; 510300(T+1) 与 513100(T+0) 同日买入交收日相差 1 个交易日 ✓; 摩擦成本 224 元/单边全仓 (佣金 74.76 + 滑点 149.29); 20 tests; 全量 226/226 通过
- **影响范围**: `src/backtest/custom_loop.py`, `tests/test_custom_loop.py`, `config/settings.yaml` (trading_cost 重构 + backtest 段); P0 Gate "回测正确性" "T+0/T+1 正确性" 由本条闭环 → P0 Gate 6/6
- **后续动作**: T1.9 vectorbt 对照 (相关系数 > 0.9999) 时复用本引擎验收口径; P1 若引入真实指数数据可加 "vs 000300" 参考报告; 回测接入 daily_pipeline 信号回放留待 P1

### EV-014 — Streamlit 信号面板 (T0.10) 与 P0 Phase 完成 🎉

- **日期**: 2026-09-13
- **类型**: 关键设计决策 / Phase 里程碑
- **与 task.md 关系**: 互补 (T0.10 实现细节 + P0 收官)
- **描述**: T0.10 面板决策与 P0 收官事实:
  - **页面结构 (用户确认口径)**: 宏观状态卡 (得分/regime/仓位上限) → 今日信号表 (代码/名称/方向/目标仓位/触发原因) → 建议目标持仓 (系统无实盘持仓数据, 展示信号 target_weight > 0 的目标视图, 空仓友好提示) → 近期走势 (建议持仓标的归一化收盘价, 首日=100, 空仓回退基准 510300) → 宏观得分趋势 (近 120 交易日)
  - **分层**: `dashboard/data_loader.py` 纯数据层 (不 import streamlit, 可单测) + `dashboard/app.py` 纯渲染; dashboard 加入 hatchling packages 实现全项目导入
  - **宏观趋势重算**: 沿用无状态设计 (EV-009) 逐日截断重算; 实测 120 天逐日 ~13s 过慢 → step=5 采样 (~2.5s), 始终包含最新交易日; 测试保留 step=1 与直接计算的一致性校验 (首尾点)
  - **依赖**: streamlit 1.59.2 从 p1 optional 组移入主 dependencies (P0-D10 交付所需, Python 3.14 兼容已验证); 图表用 streamlit 原生组件, 不引入 plotly
  - **验收**: 16 tests (含 streamlit AppTest 无头执行断言无异常 + 真实库"信号与 DB 记录一致"); headless 服务 `/_stcore/health` = ok, 首页 HTTP 200; 全量 242/242 通过
  - **P0 收官**: 交付物 10/10, Gate 6/6 全通过 → **P0 Phase 完成**
- **影响范围**: `dashboard/` (新增), `pyproject.toml` (streamlit 主依赖 + dashboard package); P0 Phase 完成
- **后续动作**: P1 启动 (T1.1 因子注册表 + 因子模块); ⚠️ P1 前置需解决 I-02 (Python 3.14 vs vectorbt/alphalens); 面板后续可扩实盘持仓录入与净值跟踪 (人工记录) 留待 P1 后评估

### EV-015 — spec v3.0 / plan v1.0 落地与 Phase 0.5 M0 初审 🔴

- **日期**: 2026-09-14
- **类型**: 重大范围变更 / 审计里程碑
- **与 task.md 关系**: 覆盖 (spec v3.0 替代旧业务口径; 旧 task 保留为历史参考)
- **描述**: spec v3.0 (v3.0 文档 + plan.md M0–M7) 正式落地, 启动 Phase 0.5。M0 初审结论:
  - **基线核实**: 分支 main, commit `4cd14c4`; 工作区干净; 全量测试 **242/242 通过** (2026-09-14 实测); DB = 19 ETFs / 34,073 rows / 2019-01-02→2026-09-11; `trade_signals` 11 条 (3 个信号日: 2026-07-24×1, 09-01×5, 09-11×5 全 HOLD); `account_state`/`macro_indicators` 空表
  - **Phase 0 按 §8.1 新口径未收尾**: T0.1~T0.10 代码与测试属实且通过, 但缺 6 组证据 → A0-1…A0-6 (策略版本、数据时间、推送失败追踪、回测独立手工期望值、分红限制标注、批次可卖/场景证据、面板模拟状态标注)
  - **C01–C23 初审完成**: 核心 A 类 = C01 (inverse_volatility 实为 1/σ²)、C04 (15% 防抖压制小仓位退出/止损)、C05 (批次可卖与现金可用/可取)、C06 (ATR 高水位非持有期、棘轮未接线)、C08 (旧滑点公式未降级、最低收费未接线)、C09 (Sina 不复权无分红)、C10 (固定 20% 阈值/NAV 静默跳过)、C11 (19 ETF 代理称市场宽度、A 股 cap 约束全部资产); C14/C18 当前路径已核查不受影响
  - **产出**: `docs/phase0.5-audit.md` (C01–C23 逐项 + §8.1 剩余项) + `docs/task-phase0.5.md` (A0 补项 + M1–M7 原子任务 + P05-D1–D8 对照 + 样本案例映射)
  - **待用户确认**: AGENTS.md §5.2/§7 旧条款修订草案 (C13/C08/C09/C19, 涉及仓库宪法文件, 按 plan §1 须审阅流程)
  - **约束遵守**: 未运行 `uv run daily` (避免写库/推送), 未改写数据; 旧 3 个信号日标注为 1/σ² 旧口径保留
- **影响范围**: 全项目 (新业务口径); docs/phase0.5-audit.md, docs/task-phase0.5.md 新接手入口
- **后续动作**: 用户审阅 M0 产物 → 启动 M1 (A 类纠偏 + Phase 0 收尾); 每小批次验证后停靠汇报

### EV-016 — M1-1: C01/C04/C08 修正 + 版本/成本基础设施 🔴

- **日期**: 2026-09-14
- **类型**: 关键修正 (spec §9 A 类) / settings 参数变更
- **与 task.md 关系**: 覆盖 (旧 T0.5 1/σ² 口径由 spec v3.0 纠正)
- **描述**: Phase 0.5 M1-1 交付:
  - **C01 修正**: `PortfolioOptimizer(method=...)` — inverse_volatility = w∝1/σ (默认, 恢复原意); inverse_variance = w∝1/σ² (v0.1 旧口径, 保留对照); `method` 标签贯穿结果与信号原因; 未知方法拒绝
  - **C04 修正**: 信号防抖 (`turnover_threshold`) 仅约束常规调整; 风险退出(止损)/明确退出(清零)/宏观骤降减仓 绕过防抖; 首次建仓 (prev=0) 绕过防抖 (spec §5.4 不被永久跳过); 原因串留痕
  - **C08 修正**: 佣金 = max(成交额×费率, 最低收费); `trading_cost.scenario="legacy_stress"` 明确命名 (旧滑点公式非校准定律); CLI 报告输出情景名
  - **版本基础设施**: `settings.project.mode="research"` + `strategy_version="v0.2"`; `PipelineResult` 增加 mode/strategy_version 并输出到 summary
  - **settings.yaml 变更记录 (原因: spec v3.0 §3/§5.3/§6.3 纠偏)**: ① project 新增 mode/strategy_version; ② `portfolio.method` 语义修正 (inverse_volatility 现为 1/σ); ③ trading_cost 新增 scenario 字段
  - **验证**: 新增 18 测试 (独立期望值: 2:1 / 4:1、往返对账 109,752.51125、最低收费、防抖绕过 6 例、ATR 暴跌集成) → 全量 **260/260**; 真实回测复跑 510300 偏差 0.044% < 1%、交收差一天 ✓; ruff 零新增
  - **旧口径保留**: 数据库 11 条旧信号 (1/σ², 3 个信号日) 不覆盖, 标注 v0.1; 新日期 run 产生 v0.2 目标 (M1-3-04)
- **影响范围**: `portfolio_optimizer.py`, `daily_pipeline.py`, `custom_loop.py`, `settings.yaml`, 3 个测试文件; `docs/phase0.5-audit.md` (C01/C04/C08 → 已修正-已验证)
- **后续动作**: M1-2 (C10/C11/C05/C06/C09 + 回测证据) → M1-3 (标注/面板/重跑收尾)

### EV-017 — M1-2: 数据质检产品化、宏观 scope、批次可卖与口径标注 🔴

- **日期**: 2026-09-14
- **类型**: 关键修正 (spec §9 A 类) / settings 参数变更 / 真实数据发现
- **描述**: Phase 0.5 M1-2 交付:
  - **C10**: validator 产品感知涨跌幅 (默认 10% 板; overrides 20% 板 159915/588000; 容差 2pp → warning; 超容差 → critical 阻断, 提示疑似权益事件); `universe_filter` 输出 `unknown` (NAV 缺失不静默); daily 传阈值并显示 unknown 数; `fetch-data` 全量导入输出质检摘要 (不阻断)
  - **C11**: "ETF 代理宽度"命名全链路 (摘要 `etf_breadth`/面板指标); `macro_timing.scope="a_share"` (默认, 宏观上限只约束 A 股合计); 非A 独立上限 `portfolio.non_a_share_max_total=0.30` (研究示例待评估); `all_composite` 保留为显式研究版本
  - **C05**: 引擎批次化 `_Lot(shares, unlock_date)` (FIFO 扣减, 顺延按最早解锁日); 现金 `cash`/`cash_withdrawable` 分离 (卖出款 T+1 可取); nav 增列
  - **C06**: 跳空独立证据 (T+1 开盘成交, 不按止损价); ATR 近似声明 (2×ATR 窗口/收盘价, 状态化归 M4)
  - **C09/A0-4**: `CALIBER_NOTE` (不复权/无分红/待 M2) 进入回测摘要与 CLI
  - **settings.yaml 变更 (原因: spec §4.4/§5.2/§5.4)**: ① 新增 data_quality 节 (默认 10%/overrides/buffer); ② macro_timing.scope; ③ portfolio.non_a_share_max_total
  - **真实数据发现 (重要, 见 I-09)**: 库内存在未调整权益事件跳变 (513100 2022-01-14 ÷5.1, 159941 2022-07-05 ÷3.9, 512010 2021-06-28 ÷3.8, 512480 2026-07-03 ÷2.0, 159995 2026-07-07 ÷2.0, 513500/159928/512690/516160/512100 等 10+ 处), 系 2026-07-19 全量 fetch-data (无校验) 写入; 每日增量含此类日会阻断 (隔离设计, 不得用全量绕过); 另 588000/159915 2024-09-30/10-08 ±20% 为真实 20% 板移动 (新规则限内通过)
  - **验证**: 新增 16 测试 → 全量 **276/276**; 真实回测偏差 0.044% + 交收差一天保持; 真实数据副本干跑 (cap=0 时 A 股全 0 / 非A 按独立上限参与 / 19 只 unknown 显示 / mode+版本+scope 输出)
- **影响范围**: validator, universe_filter, macro_timing, daily_pipeline, custom_loop, akshare_fetcher, settings, dashboard; audit C05/C06/C09/C10/C11
- **后续动作**: M1-3 (C03 标注/面板模拟、A0-1/A0-2 推送与数据时间、C13 草案、策略重跑 v0.2、§8.1 收尾核对)

### EV-018 — M1-3: 模拟标注、Schema v2 与 M1 收尾 ✅

- **日期**: 2026-09-14
- **类型**: 关键修正 (C03 部分/A0-1/A0-2/A0-6) / schema 迁移 / Phase 0 收尾
- **描述**: Phase 0.5 M1-3 交付:
  - **标注 (C03/A0-6)**: daily 摘要含 mode/版本/数据截止/scope; 推送头部"🧪 research | 策略版本 | 数据截止 | 无真实账户接入, 非成交/持仓记录"; 面板顶部模拟横幅 + 字幕 + "版本"列 + "策略目标持仓（模拟，非实际持仓）"
  - **推送 (A0-2)**: 结构化留痕 `push_result ok/channel/attempts/error`（M2 转运行记录持久化）
  - **Schema v2**: `trade_signals.strategy_version`; 迁移只增列+旧记录标注 v0.1（不覆盖事实）; `upsert_signals` 兼容旧调用且 COALESCE 保护既有版本
  - **真实库迁移 (plan §5)**: 备份 `data/backups/quant_pilot_20260914.duckdb` + 迁移前信号 CSV → 隔离副本演练 → 真实库迁移（11 条旧信号 → v0.1, 内容逐行不变, schema_version [1,2]）→ 副本真实网络重跑（09-11 v0.2, 旧日 v0.1; 当前无新交易日数据, 真实库 v0.2 待下一交易日 daily 自然生成）
  - **§8.1 收尾**: 证据清单 7 行全部闭合（推送/面板行、模拟状态行 → ✅）
  - **C13**: AGENTS.md 修订草案提交至审计 §4.1（7 处条款对照），**待用户确认后更新本体**
- **验证**: 全量 **280/280**; ruff 零新增; 迁移前后 counts/sum/行内容比对一致
- **影响范围**: notifier, daily_pipeline, dashboard, duckdb_manager, akshare_fetcher(fetch-data 提示), tests; audit §1/§4
- **后续动作**: 用户确认 AGENTS 修订 → **M2**（账户/四类记录/schema 迁移 v3）

### EV-019 — AGENTS.md v3.0 修订（C13 关闭）🔴

- **日期**: 2026-09-14
- **类型**: 仓库规则文件修订（用户确认后执行）
- **描述**: 按 spec v3.0 与审计 §4.1 对照表更新 `AGENTS.md`（v2.0 → v3.0）:
  - §5.2 #2 收益率口径按用途声明（组合核算用算术收益）; #3 价格口径按用途分离（研究序列/成交账本/复权版本化, 不复权限制标注）; #4 成本按实际或标注研究配置（旧滑点公式=命名旧压力情景 legacy_stress; 不得二次扣费）; #5 连续/布尔信号均可（须声明、可复现、经验证; 风险退出不被带宽压制）; #6 因子须平稳化/可比（ADF 等按用途采纳）
  - §7 检查清单 #2–#5、§8 禁止事项 #4 同步; §10 追加 v3.0 版本记录行
- **影响范围**: `AGENTS.md`（宪法级）; 后续所有批次开发口径; 审计 C13 → **已修正-已验证**; I-04/I-07 相应关闭
- **后续动作**: M2 起以 AGENTS v3.0 + spec v3.0 为准实施

### EV-020 — M2a: 账户账本、schema v3 与运行记录 🟡

- **日期**: 2026-09-14
- **类型**: M2 里程碑（账户层）/ schema 迁移
- **描述**: M2a 交付:
  - **事件契约**: `src/data_pipeline/accounting.py`（9 类事件; 现金可用≠可取; 更正替换语义; 确定性去重 ID; 批次可卖）
  - **schema v3**: 11 张新表（账户/事件/批次/快照/目标/建议/反馈/运行/实验/迁移日志）; `schema_migration_log` 记录演练核对摘要; 真实库已迁移（pre_v3 备份 + 副本演练 + 幂等; 内容不变）
  - **统一写入口**: `AccountService.import_events` 同一事务（插入→回放→批次/快照）; 失败回滚不留半笔
  - **用户录入通道**: 标准 CSV（模板 `docs/account_events_template.csv`）+ CLI `uv run account-import`; 重复导入自动去重（演示: 首次新增 10, 二次跳过 10）
  - **运行记录**: daily 流程写 runs（结果/版本/状态）; 推送结果持久化 `push_ok/attempts/error`（失败/阻断运行也留痕）
- **验证**: 28 项新测试 → 全量 **308/308**; ruff 与基线持平（新增代码零告警）; 真实库迁移演练与 CLI 演示通过
- **影响范围**: accounting.py（新）, duckdb_manager, daily_pipeline, pyproject（account-import）, docs 模板; audit C20/C21 部分进度
- **后续动作**: **M2b**（target/proposal/feedback + 部分成交/未执行样本 + 并发协调说明）; 用户可开始按模板录入真实账户（可选）

### EV-021 — M2b: 目标/建议/反馈落库，M2 完成 🟡

- **日期**: 2026-09-14
- **类型**: M2 里程碑（四类记录闭合）
- **描述**: M2b 交付:
  - **Targets**: daily 写 `targets` + `target_weights`（完整权重含现金 `__CASH__`，Σ=1；版本字段 strategy_version/mode/data_version/pool_version(标的集哈希)/config_version；原因含宏观/scope/风控）
  - **Proposals/Feedback**: `src/orchestration/proposal_ledger.py` — 建议去重落库；状态机 proposed→partial→filled / rejected / expired / replaced；反馈幂等（feedback_id）；成交类反馈联动账户入账（确定性 event_id，可 `apply_to_ledger=False` 关闭以避免与直录事件重复）；超额成交保留事实并告警；非系统交易可入账；新目标生效自动替换旧建议（`mark_targets_replaced`）
  - **用户通道文档**: README「账户事件录入」节（CSV/CLI、单通道原则、单写进程约束）
- **验证**: 15 项新测试 → 全量 **323/323**; ruff 新增文件零告警; 真实数据副本干跑（target pool_version=universe-19etf-abc0b4d5, weights Σ=1, cash=1.0, run ok）
- **影响范围**: proposal_ledger.py（新）, duckdb_manager（目标/建议/反馈方法）, accounting（事务参数）, daily_pipeline（_step_persist_target）, README; audit C03/C20
- **后续动作**: **M3**（数据时点与 ETF 四层池）; 用户可按 README 录入真实账户（可选，未录入时保持 research 模式）

### EV-022 — M3: 四层池、产品契约与 NAV 门槛 🟡

- **日期**: 2026-09-14
- **类型**: M3 里程碑（数据时点与 ETF 池）/ settings 参数变更 / **行为变更**
- **描述**: M3 交付:
  - **schema v4**: `instruments`（19 只: 交易所/市场类型/资产类别/指数名/上市日近似/结算/涨跌幅限制/溢价必需标记）、`universe_versions`、`universe_members`、`nav_daily`；真实库已迁移（pre_v4 备份 + 副本演练 + 应用）
  - **四层池** `src/strategies/etf_pool.py`：分项检查 pass/fail/unknown/not_applicable；can_buy/can_sell/can_hold；**禁买 ≠ 清仓**；停牌（stale）禁买卖、持仓保留并按最近价估值；池版本内容哈希 `uv-…`；出池仅标注区间不删历史
  - **历史池判定**：`member_buyable_at`（上市日前不可买；版本声明日前标 `conditional_research`）
  - **产品对照** `src/strategies/product_compare.py`：相关矩阵（共同交易日/窗口/样本/跳过原因）、同指数分组、对照表；不按阈值自动删池
  - **行为变更（重要）**：缺 NAV 且溢价检查必需（默认跨境）→ **禁新买**（unknown 记录）；真实干跑：19 只中 **14 可买、5 只跨境禁买**；停牌持仓信号改为 **HOLD 保留原目标**（不盲目 SELL）
  - **settings.yaml 变更**（原因: spec §4.3/§4.4）：universe 条目新增 `index_name`（19 只研究元数据）；universe_filter 新增 `premium_check_required_market_types: ["跨境"]`
- **验证**: 23 项新测试 → 全量 **346/346**；ruff 新增文件零告警；真实库 v4 迁移与真实数据干跑验证
- **影响范围**: etf_pool.py / product_compare.py（新）, universe_filter（结构化 evaluate）, daily_pipeline（池报告接入/停牌 HOLD）, duckdb_manager, settings; audit C12/C15
- **已知限制**: 跨境新增买入被 NAV 门槛阻止 → 解锁需 NAV 数据接入（I-03）；复权修订机制随权益事件（M2 扩展/P1）落地
- **后续动作**: **M4**（目标→建议统一：账户对比、数量规划、优先级、重新进入）

### EV-023 — M4: 调仓规划器与状态化 ATR 止损 🟡

- **日期**: 2026-09-15
- **类型**: M4 里程碑（目标→建议统一）/ schema 迁移 / settings 参数变更
- **描述**: M4 交付:
  - **`rebalance_planner.py`（纯函数）**: 账户估值 `value_account`（NAV=现金+应收+Σ份额×最近可用收盘价; 缺估值价→`paused`, 不静默按 0）；`plan_rebalance` — actual vs target 偏差、绝对百分点 band（`rebalancing.band_points=0.05` 示例）、数量规划（整手 100、佣金 max(率×额, 最低)、滑点估算不重复扣现金）、可卖份额限量、现金分配（卖出先释放; 满额请求按执行顺序分配, 不足缩减并留原因）、普通调整最小金额过滤（不压制风险退出）、卖出依赖 `depends_on` 条件建议、有效期 `valid_days`、四级优先级（风险退出 90 > 常规卖出 50 > 常规买入 30 > 依赖买入 10）
  - **`strategy_states.py`（C06 关闭）**: 持有期高水位（entry_date 起, 不再是固定 2×ATR 回看窗口）+ 棘轮止损（`enforce_stop_ratchet` 接线, 只上移）+ `pending_exit`（触发未卖出持续提示, 份额归零后转冷却）+ 冷却 `cooldown_until`（阻断新买）+ 重新建仓重置; `RiskManager.calc_atr_series`（Wilder 完整序列, 与 `_calc_atr` 同源）
  - **schema v5**: `strategy_states` 表（pre_v5 备份 + 副本演练 + 真实库迁移, 内容不变, 幂等）
  - **daily Step 10**: `rebalance_run.py` — 账户存在且 `rebalancing.account` 配置时执行（事件回放→状态更新→规划→建议落库）; 建议 ID 与目标 ID 按决策日确定（同日重跑确定性去重, 不重复累计）; 新决策日旧开放建议 `replaced`; 失败不阻断 daily（留痕 runs.detail_json）
  - **settings.yaml 变更（原因: spec §5.4/§5.5/§6.1）**: 新增 `rebalancing` 节（account 默认 null=不生成建议; band_points 示例 0.05; min_trade_amount 500; valid_days 3; cooldown_days 5; lot_size 100; cash_reserve 0）
- **验证**: 46 项新测试（planner 30 + 状态 14 + daily 集成 2 净增）→ 全量 **395/395**; ruff 零新增（存量 UP045 除外）; 真实数据副本干跑: ①账户触发状态化止损（510300/518880）→ 优先 90 SELL 不受带宽压制; ②510500 实际 27.4% → 卖 900 份调回 20%（band）; ③同日重跑 0 新增/2 去重
- **影响范围**: rebalance_planner.py / strategy_states.py / rebalance_run.py（新）, risk_manager（序列化 ATR）, daily_pipeline（Step 10/摘要/运行记录）, proposal_ledger（mark_stale_replaced）, duckdb_manager（v5/entry_dates/替代查询）, settings; audit C03/C04/C05/C06/C07/C11/C20/C21
- **已知限制**: `rebalancing.account` 为空时 daily 不生成建议; 现金顺序分配非全局最优（差额留待下一轮）; 回测引擎未接状态化 ATR（P1 登记）
- **后续动作**: **M5**（B0/B1 基准 + 主动策略共用规划与执行基础, 独立模拟账户）

### EV-024 — M5: B0/B1 基准模拟、实验记录与同口径对照 🟡

- **日期**: 2026-09-15
- **类型**: M5 里程碑（基准与对照接入）/ settings 参数变更
- **描述**: M5 交付:
  - **`backtest/baseline.py`**: B0 固定初始配置买入持有（首日盘后下单 → 次日开盘成交, 此后不调整）; B1 固定目标 + 绝对百分点 band（检查日 = 每周最后交易日, 用**模拟实际份额**计算漂移, 整手向最近调回）; 执行基础与实盘一致（T+1 开盘/卖出先于买入/批次可卖/整手 100/佣金 max(率×额, 最低)/滑点含价/现金不足缩减）; 逐笔费用与事件留痕; 无外部现金流为默认口径, 入金按"同额同日"预先声明（B0 政策 `hold_cash` 显式）
  - **`backtest/baseline_runner.py` + CLI `uv run baseline`**: 固定研究配置（`settings.baselines`）执行 → `experiments` 表记录（kind/代码/数据/池/配置 JSON/成本情景/结果位置/状态/结论）→ NAV 序列与摘要落 `data/backtests/`; 实验 ID 含成本情景名（不同情景不互相覆盖）
  - **settings.yaml 变更（原因: spec §5.1/§5.4/§6.3）**: 新增 `baselines` 节 — **研究示例**（来源 docs/讨论2.md"股 70%/黄金 10%/现金 20%"）: 6 只股票 ETF 合计 70% + 518880 黄金 10% + 现金 20%; band_points=0.05; initial_cash=100000（与回测一致）; `b0_cash_policy=hold_cash`
  - **schema**: 无新表（experiments 由 v3 DDL 承载）; `upsert_experiment`/`get_experiment` 新方法
- **验证**: 16 项新测试（B0 手工余额核对、B1 漂移调回 600 份手算、检查日、T+1 不可卖、滑点含价、同口径/确定性、实验记录与场景、入金政策、同执行基础）→ 全量 **411/411**; ruff 零新增; 真实数据副本（`rehearsal_m5.duckdb`, 2019-01-02→2026-09-11）: **B0 +104.72% / 回撤 −31.04% / 7 笔 / 费用 58.75 元; B1 +102.16% / 回撤 −29.84% / 14 笔 / 费用 112.36 元**（再平衡降回撤、增费用）; 入金测试（2024-06-03 各 20000 元）两账户独立
- **影响范围**: baseline.py / baseline_runner.py（新）, duckdb_manager（experiments 方法）, settings, pyproject（baseline CLI）, README（基准运行说明）; P05-D4 闭合, D6/D7 部分
- **已知限制**: 真实数据含未调整拆分跳变（I-09）→ 拆分年（如 2022）可能触发非策略性再平衡, 报告须标注干扰；B0/B1 按不复权口径比较（CALIBER_NOTE）
- **后续动作**: **M6**（基础绩效/入金不计收益/贡献闭合、实验记录补全、面板扩展与净值对照）

### EV-025 — M6: 基础绩效、实验追踪补全与面板扩展 🟡

- **日期**: 2026-09-15
- **类型**: M6 里程碑（绩效与展示）/ schema 迁移
- **描述**: M6 交付:
  - **`backtest/performance.py`**: `compute_performance` — 事件驱动逐日估值; **金额核对**: 期末 − 调整起点 − 净外部入金 = 投资盈亏; 分项 持仓/现金贡献、分红、费用单列, 未解释差额显式（应为 0）。调整起点约定: 起点日收盘权益 + 当日费用 − 当日分红（起点日资本不进收益区间, 当日费/红利计入期间且不重复）。**入金/出金不计收益**; 分红计收益; 费用只以 fee 事件进入现金（参考价滑点不二次扣）。TWR 日频链接（现金流按日终估值点近似, `CASHFLOW_TIMING_NOTE` 随报告）。`performance_for_account`（账户路径）、`load_nav_series`（逐日净值）、`compare_accounts`（多账户独立对照, 首日=100）
  - **schema v6**: experiments 增列 `mode/started_at/finished_at/error`（副本演练 + 真实库迁移, counts 不变）; 基准 runner 写模式与起止时间; **失败试验留痕**（status=failed + 错误摘要, 不删除）
  - **面板扩展（C21 只读连接）**: `dashboard/data_loader` 新增 `load_account_summaries` / `build_nav_comparison` / `build_actual_vs_target` / `load_pending_proposals` / `load_latest_run`; 全部读取改用 `read_only=True` 连接（不写库, mtime 不变测试）; `app.py` 新增 实际 vs 目标、待处理建议与反馈、净值对照（B0/B1/A 独立）、运行状态/推送/失败提醒
- **验证**: 21 项新测试（绩效 14: 闭合/入金/出金/分红/费用/缺口/多账户/与 B0 联动独立期望; 基准失败留痕 1; 面板 6）→ 全量 **432/432**; ruff 新文件零告警; 真实数据副本干跑: B0 期末 204,721.94（+104.72%, unexplained 0）、B1 期末 202,157.43（+102.16%, unexplained 0）, 与 M5 模拟器一致; 面板摘要与净值对照（1,868 行）正常
- **影响范围**: performance.py（新）, baseline_runner（失败留痕/模式/起止）, duckdb_manager（v6/experiments 列）, dashboard（data_loader/app）, tests; audit C21/P05-D6/D7/D8
- **已知限制**: TWR 为日频近似（现金时点=日终）, 只有更细现金流时间才可精确; 估值缺口存在时金额核对不可用（显式提示）
- **后续动作**: **M7**（迁移/重跑/恢复演练、关键场景证据、对照 P05-D1–D8 验收、README 与交接收尾）

### EV-026 — M7: 隔离副本演练、集成验收与 Phase 0.5 收官 🎉🔴

- **日期**: 2026-09-15
- **类型**: M7 里程碑（迁移演练/验收/交接）/ Phase 0.5 完成
- **描述**: M7 交付:
  - **隔离副本演练** `scripts/rehearsal_m7.py`（报告 `data/backups/rehearsal_m7_report.json`, ALL_OK=True）:
    ① **迁移幂等**（副本重复 `init_database` 两次; counts/signals 逐项不变; versions=[1..6]）
    ② **半笔事件保护**（卖超 → `AccountError`; 事件表与回放状态零变化）
    ③ **重跑幂等**（同事件集二次导入 0 新增/全部去重）
    ④ **恢复路径**（备份 → 新事件 → 恢复 → 重放新事件 → 现金/份额与事件指纹一致; plan §5.8: 切换后新真实事件必须重放核对, 不能简单丢弃）
    回归测试 `tests/test_rehearsal.py` 5 例（临时目录, 不触碰真实库）
  - **关键场景证据**: `docs/phase0.5-audit.md` §5 — plan §6 十三条 + spec §8.2 十样本案例逐项证据（测试名/命令/记录）; **P05-D1–D8 全部闭合**（§5.3）; 引用测试名/类名经 grep 全量存在性复核
  - **文档**: README 重写为实际运行/导入方式（命令表/账户录入/基准与绩效口径/数据问题与安全边界）; task/审计/记忆/HANDOFF 收尾
  - **已知限制与恢复路径**（HANDOFF 专节）: I-02/I-03/I-09 与登记项（C02/C16/C17/C22/C23 → P1/P1.5）、恢复方法（备份恢复 + 事件重放核对）、操作约束（单写进程/勿全量 fetch-data 绕过阻断）
- **验证**: 全量 **437/437** 通过（M7 新增 5 项）; 真实库 schema v6 保持（0 账户/0 建议, 研究状态）; 演练报告 ALL_OK=True
- **影响范围**: scripts/rehearsal_m7.py（新）, tests/test_rehearsal.py（新）, README, docs（task/审计/记忆/HANDOFF）
- **收尾结论**: **Phase 0.5 完成**（工程 Gate + P05 验收齐全）; **≠ 允许实盘、≠ 自动进入 Phase 1**; 真实账户启用 Gate 未触发; 下一步 Phase 1 工单由用户确认后再拆解（少量候选因子验证; 前置 I-02 Python 版本决策）
- **后续动作**: P1（先解决 I-02: `uv python pin 3.12` 或隔离环境; 再拆少量因子/模块对照任务）

### EV-027 — M8: 权益事件登记与拆分感知质检（I-09 方案1）🟡

- **日期**: 2026-09-20
- **类型**: 用户确认追加批次（数据质量问题处置）/ schema 迁移
- **背景**: `uv run fetch-data` 全量质检报出 13 条 critical（未调整权益事件跳变, I-09）; 用户要求实施"方案1"（登记 + 拆分感知质检, 不修改价格数据）; **方案2（权益事件入账）归入 Phase 1**（用户确认, task §10）。
- **描述**: M8 交付:
  - **schema v7 `equity_events`**: symbol/event_date/event_type/ratio/ratio_semantics(price_factor: 后一收盘 ≈ 前一收盘 / ratio)/source/inferred/verified/method/note; 副本演练（`rehearsal_v7`）+ pre_v7 备份 + 真实库迁移（counts 不变, 幂等）
  - **拆分感知质检**: `validate_ohlcv(registered_equity_events=...)` — 命中登记日期且比例一致（|实际−期望| ≤ 2pp 或 5%×期望）→ `registered_equity_event`（info, 不阻断）; 未登记 → 维持 critical; **比例不符 → critical**（防"洗白"二次修订）; daily 与 fetch-data 两条路径均接入
  - **登记脚本** `scripts/register_equity_events.py`: 产品感知扫描（20% 板 ±20% 不误报）、`--check`/`--apply`/`--auto`、幂等登记、**登记后自动复核（同一标的残余 critical → 事务回滚）**
  - **真实库登记 12 条**（全部 `inferred=True, verified=False` 推断值待核验）: 159928/512010/512690×2/512480×2/513100/513500/159941/516160/159995/512100; 其中 512690 2021-12-31 由"前收 1.323 → 当日开盘 0.976, 缺口全部在开盘且成交量正常"判定为份额折算（非数据错误）; 510500 2022-08-29（−12.74%, 幅度小）不登记, 维持人工核验
- **验证**: 10 项新测试（拆分感知 7 + 持久化 2 + 脚本 2 净计）→ 全量 **447/447**; ruff 新文件零告警; 效果: 全量 critical 12→**1**（仅 510500 边界项）、拆分窗口（2026-07-03/07-07）2→**0 critical + 2 info**、daily 干跑（副本, 数据至 2026-09-18）正常完成
- **影响范围**: duckdb_manager（v7/equity_events CRUD）, validator（registered_equity_events）, daily_pipeline/fetch-data（登记表接入）, scripts/register_equity_events.py（新）, tests/test_equity_events.py（新）; README/审计/HANDOFF
- **已知限制**: 登记比例为价格反推推断值, 待官方数据/人工核验升级（`verified` 标记）; 512100 因子偏大待优先核验; **方案2（账户入账, 真实持仓前必须完成）在 Phase 1**
- **后续动作**: Phase 1（用户确认后; 含 I-09 方案2、I-02 版本决策）

### EV-028 — Phase 1.1 启动：任务清单、用户确认与 P11-M0 完成 🟡

- **日期**: 2026-09-26
- **类型**: 阶段启动 / 任务初始化（用户确认后执行）
- **与 task.md 关系**: 互补（spec v3.1 §8.4 新增 Phase 1.1；task.md 原路线无此阶段）
- **描述**: Phase 1.1（单标的探索、观察名单与规则诊断）启动:
  - **基线核对（实测）**: 全量 **447/447** 通过（31.6s）; schema v7; 19 ETFs / 34,168 行（2019-01-02→2026-09-18）; 0 账户/建议/实验; M6–M8 未提交改动保留未丢弃
  - **环境核实**: Python 3.14.3; akshare 1.18.64; streamlit 1.59.2; altair 6.2.2 已装; **plotly 未装**（p1 可选组, dry-run 可装 7.1.0）; `scripts/validate_registry.py` 缺失（factor-check 入口悬空, 登记归 P1-R2）
  - **产出 `docs/task-phase1.1.md`**（40 项任务, P11-M0–M6）: §1 差异核对（可复用/待补/限制）; §5 设计决策 D1–D8（plotly 选型; 非池行情独立表 `diag_price_daily`; 身份缓存独立表 `securities`; 诊断指标注册于 factor_registry 新段; 快照 JSON 列; 刷新复用 validator 不绕过质检; 未核验权益事件→跨事件指标暂停; schema v8 五表草案）; §6 Phase 1 研究任务独立登记（P1-R1–R6, 不被本阶段验收）; §7 v8 表结构草案; §8 P11-A1–A10 映射; 附录 A 接口调查模板 / 附录 B 规则卡草案
  - **用户确认**（2026-09-26）: 任务清单无调整, 指示实施 M0
  - **P11-M0-02 文档残留修正**: `task-phase0.5.md` L106 M2 标题（"M2b 待启动" → "✅ 完成 2026-09-14, M2a+M2b"）; L278 入金样本行（"未实现（M6）" → "✅ M6 完成（零收益+入金 → 收益率 0; `test_zero_price_return_deposit_does_not_raise_return`）"）; diff 仅此两处
- **影响范围**: `docs/task-phase1.1.md`（新增, 后续 P11 实施以此为准）; `docs/task-phase0.5.md`（两行状态残留修正, 不改变历史事实）; Phase 1 研究仍独立（phase1_kickoff.md / P1-R1–R6, 不因 P11 完成注销）
- **后续动作**: **P11-M1 接口实测**（核心行情/身份/日历/财务/估值/ETF 扩展字段, 先行批次; 结论固化附录 A: 可用/部分/暂缓）→ M2 schema v8 → M3 指标/计分 → M5 页面 → M6 验收

### EV-029 — P11-M1：36 项接口实测与能力表固化 🔴

- **日期**: 2026-09-28
- **类型**: 先行批次完成 / 接口能力调查（决定后续实现范围）
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段; task.md 无此阶段）
- **描述**: P11-M1 完成（3 轮真实调用共 36 项, 探针 `scripts/probe_phase11_interfaces.py`, 原始输出 `data/backups/probe_phase11_20260928_*.json`）:
  - **可用（进入实现）**: ETF 日线 `fund_etf_hist_sina`（510300/513100, 最新 09-28, 0.3–0.6s）; 股票日线 `stock_zh_a_daily`（600519 raw/qfq 6013 行, 688981 短历史, 含 outstanding_share/turnover）; 身份 `stock_info_a_code_name`（5569, 含北交所）+ `fund_etf_category_sina`（1687）+ `fund_name_em`（27991 辅助）; 日历 `tool_trade_date_hist_sina`（8797 行 1990→2026-12-31）; 财务 `stock_financial_report_sina` 三大报表（**含报告日/公告日期/币种/是否审计**; 金融企业结构不同）+ `stock_financial_analysis_indicator`（86 列）; 估值 `stock_value_em`（日频 13 列 PE/PB/市值, 2018 起）主 + `stock_zh_valuation_baidu`（近一年）备; ETF 资料/费率/规模 `fund_overview_em`（管理费 0.15%/托管费 0.05%/净资产规模/跟踪标的）; 净值历史 `fund_open_fund_info_em`（510300 3502 行）; 折溢价快照 `fund_etf_fund_daily_em`（1669 行含折价率）; 深市份额 `fund_etf_scale_szse`
  - **部分可用 + 降级**: 东财备用路径偶发断连（RemoteDisconnected/ProxyError, 1–2 次实测失败）→ 备用源 + 退避重试; 沪市份额 `fund_etf_scale_sse` 近期日期失败（仅历史期可用, 默认 2025-01-15）; 折溢价跨境当日净值可能缺失（513100 实测 `---`）→ **日期/币种不匹配一律 unknown, 不解除既有禁买**; `stock_individual_info_em` 不作唯一依赖
  - **暂缓（明确记录, 不阻塞核心交付）**: ETF 跟踪质量（缺可比全收益指数, 不以价格指数/高相关替代）; ETF 底层估值映射（缺可靠映射, 黄金/债券不适用）; 雪球资料接口（本环境 KeyError）; `fund_fee_em` 空表（费率以 `fund_overview_em` 为准）
  - **接口故障登记**: `fund_etf_fund_info_em` ValueError（本版本损坏→用替代）; `stock_a_indicator_lg` 不存在（→ `stock_value_em`）; 错误代码行为（ETF 空表 / 股票 `KeyError('date')` → 服务层归一）
  - **口径细节**: Sina 财务值为字符串且缺失为 `'None'` 文本（需转换）; 股票 qfq 为"当前复权"口径（修订需重建, 归属 M2-04）; 财务公告日期可支撑披露期展示（历史 PIT 需快照版本化, 登记）
- **影响范围**: `docs/task-phase1.1.md`（M1 任务完成 + 附录 A 实测能力表 + §5.2 Q1–Q3 关闭 + M2-03/M4 接口选型）; `scripts/probe_phase11_interfaces.py`（新, evidence 工具）; 后续 M2/M4 实现按能力表范围执行, 暂缓项不隐藏不编造
- **后续动作**: **P11-M2**（schema v8 五表迁移 + 身份解析 + 统一取数 + 权益事件价格处理）→ M3 指标/规则卡/快照 → M5 页面 → M6 验收

### EV-030 — P11-M2：schema v8、身份解析与统一取数服务 🔴

- **日期**: 2026-09-28
- **类型**: 阶段批次完成（存储迁移 + 服务层）/ schema 迁移 / settings 参数变更
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段）
- **描述**: P11-M2 完成:
  - **schema v8（五表）**: `securities`（身份缓存）/ `watchlist` + `watchlist_changes`（观察名单与变更记录, 软删除）/ `diag_price_daily`（非池标的研究行情缓存）/ `diagnostic_snapshots`（诊断快照, 确定性 ID `symbol|as_of|rules|price` + UNIQUE）。迁移按 plan §5: 备份 `quant_pilot_20260928_pre_v8.duckdb` → 副本演练（`scripts/rehearsal_v8.py`, 23 张既有表 counts 稳定）→ 真实库应用（versions [1..8], counts 稳定, `apply.ok=true`）→ `schema_migration_log` 留痕
  - **`src/data_pipeline/securities.py`**: 代码规范化/交易所与类型前缀推断（候选, 权威以名录为准）/ `SecurityResolver.resolve|search` / 名录缓存（股票全表 + ETF 列表, TTL 内不重复联网; 未命中且过期时一次强制重载）/ `resolver_from_settings`; 未知代码 → `UnknownSymbolError`（不猜测）
  - **`src/data_pipeline/diag_data.py`**: `ensure_research_prices(refresh=none/auto/force)` — 池内 ETF 读写 `ohlcv_daily`（含出池旧持仓: ohlcv 有历史即池内路径）; 非池标的读写 `diag_price_daily`（**股票 hfq 全量替换**避免前复权拼接污染, 设计修正记录于任务文件; ETF 不复权增量 UPSERT）; 超时/退避重试（KeyError 已知失败不重试）/ 新鲜度优先 / 失败保留旧数据（stale/failed + notes）; **validator 质检门**（权益事件感知, critical 阻断不写入）; 交易日历（sina + 池内并集回退）+ `last_completed_trading_day`（cutoff 18:30 可配）; `check_equity_events`（ok/paused/data_issue/not_applicable + 已核验事件调整序列, 仅调 OHLC）
  - **settings.yaml 变更（原因: P11-M2 诊断数据参数, 非交易参数）**: 新增 `diagnostics` 节 — min_history_days=320 / refetch_buffer_days=10 / fetch_timeout_seconds=30 / fetch_retries=3 / fetch_backoff_base=2.0 / session_close_cutoff=18:30 / securities_cache_ttl_days=30
  - **验证**: 新增 71 项测试（test_diag_storage 18 / test_securities 17 / test_diag_data 36）→ 全量 **518/518**; 真实副本冒烟（`rehearsal_m2.duckdb`）: 600519 hfq updated 6013 行; 513100（池内跨境）increment 至 2026-09-28 且 `paused`（1 条未核验事件）; 交易表零变化（accounts/trade_proposals 0→0, universe_members 19→19）
  - **既有测试适配**: 3 处硬编码 schema 版本断言改为 `range(1, SCHEMA_VERSION+1)`（test_duckdb_manager ×2, test_rehearsal ×1）
- **影响范围**: `src/data_pipeline/securities.py`、`src/data_pipeline/diag_data.py`（新）; `duckdb_manager.py`（v8 DDL/迁移/CRUD）; `config/settings.yaml`（diagnostics 节）; `scripts/rehearsal_v8.py`（新）; 3 个测试文件; 后续 M3/M5 直接复用本服务层
- **后续动作**: **P11-M3**（指标函数库 → 规则卡冻结 → 计分/三态 → 解释模板 → 快照读写 → `diagnosis_service` 统一入口）→ M4/M5 → M6 验收

### EV-031 — P11-M3：指标库、冻结规则卡、计分/快照与统一诊断服务 🔴

- **日期**: 2026-09-29
- **类型**: 阶段批次完成（研究示例规则冻结 + 诊断服务）/ 设计修正
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段）
- **描述**: P11-M3 完成:
  - **指标库 `src/factor_engineering/technical.py`**: MA/偏离/斜率、12-1 精确端点区间动量、ATR/ATR%（复用 `RiskManager.calc_atr_series`）、20 日对数收益波动率（ddof=1）+ 经验分位、RSI（Wilder；零涨→100/零跌→0/双零→50）、布林带（总体标准差 ddof=0; 带宽 0 → 位置空值）、量额活跃度与 ATR% 相对基准（基准 shift(1)）; `compute_indicators(params)` + `indicator_columns(params)`（窗口不硬编码）
  - **规则卡冻结 `config/diagnostics.yaml`**（rules_version=**diag-v0.1-research**, 冻结于 2026-09-28, 编写后未再修改）: 计分项全为必需（缺失→总分为空, 缺失不视为低风险）; 组上限 trend 30 / momentum 10 / volatility 25; 组合上限 trend+momentum=35（比例折减保持项级可追溯）; 归一化 `score=min(raw,60)/60×100`; 分档 <30 正常 / 30–60 警戒 / ≥60 防御; 升级需连续 2 运行日、恢复 1 日; 展示项（RSI/布林位置/量额比等）不参与总分（不强行方向预测）; 事件敏感列清单
  - **注册**: `config/factor_registry.yaml` 新增 `diagnostic_indicators:`（D01–D12; 含公式/口径/单位/窗口/最低样本/缺失策略/用途/代码入口; 与 `factors:` 段分离, 不参与 IC 评估）; 一致性测试保证条目可导入可调用
  - **计分与解释 `src/strategies/diagnosis.py`**: `assess_row` 分项→贡献→组/组合上限→归一化→原始三态; 解释模板 tmpl-v0.1（当前值→比较/规则→结论→贡献→数据日期/来源; 无 LLM）; 展示项提示为"关注"语义
  - **快照状态机 + 统一服务 `src/orchestration/diagnosis_service.py`**: `run_diagnosis` 单入口（身份→取数→权益事件→指标→计分→状态机→快照）; trigger 全词表（初始/升级待确认/新增警戒/新增防御/恢复/维持/版本变化/数据失效/不可评估）; 同日重跑与更早运行日比较（不与自己对话, trigger 稳定）; payload/history/列表读取接口
  - **设计修正（记录理由）**: `price_version` 由 `源|口径|最后日期` → `源|口径` — 原含日期导致每个新交易日被状态机误判"版本变化"; 诊断日期由快照 `as_of` 承载（唯一键含 as_of, 幂等不受影响）
  - **验证**: 新增 66 项测试（technical 29 / diagnosis 23 / diagnosis_service 14）→ 全量 **584/584**; 真实数据副本端到端（`rehearsal_m3.duckdb`, 数据至 2026-09-29）: 600519 → 评分 37.7 **警戒**（5/5 完整, trend 17.16/momentum 5.45）; 510300 → 23.9 **正常**（RSI 29.9 超卖提示/布林下轨/缩量提示）; 513100 → 事件暂停 **不可评估**（score=None）; 同日重跑 snapshots 3→3; 交易表零变化
- **影响范围**: `src/factor_engineering/technical.py`、`src/strategies/diagnosis.py`、`src/orchestration/diagnosis_service.py`（新）; `config/diagnostics.yaml`（新, 冻结）、`config/factor_registry.yaml`（注册段）; `diag_data.py`（price_version 修正）; 4 个测试文件（含 test_diag_data 更新）; M5 页面直接消费 `run_diagnosis`/payload
- **后续动作**: **P11-M4/M5**（条件检查按 M1 能力表; 三入口+K线+刷新）→ M6 验收; 规则卡如需调数值 → 新 `rules_version`（当前冻结版本不再修改）

### EV-032 — P11-M5：三入口诊断页面、观察名单与六个月 K 线 🎉版本级

- **日期**: 2026-09-29
- **类型**: 阶段批次完成（用户可见功能交付）/ 依赖变更（plotly 入主依赖）
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段）
- **描述**: P11-M5 完成（单标的诊断页面可用）:
  - **依赖**: `uv add plotly`（6.9.0 入主依赖; p1 组同步移除重复声明; D1 决策落地）; user_guide/README 增"单标的诊断"使用说明
  - **数据层 `dashboard/diagnostics_data.py`（只读, 可单测）**: `months_before`（月末截断）; `read_symbol_prices`（池内优先/非池回退）; `build_kline_frame`（**全历史计算 MA/布林后裁剪近 6 个自然月**, 显示与计算分离）; `load_holdings`（账户事件回放 + 最近可用价估值 + 实际权重; 无账户不生成伪持仓）; `load_watchlist_rows`; `load_snapshot_details`（最近/次近）; `build_list_rows`（排序 防御>警戒>不可评估>正常>未评估; 较上次变化; 原因优先级 缺项>提示>状态）
  - **渲染层 `dashboard/diagnostics_app.py`**: 我的持仓/观察名单/探索 三视角 → 详情（评分卡/问题清单/Plotly 蜡烛图+量额副图+MA/Boll 开关/指标与贡献/模板解释）; **自动更新冷却**（打开先读快照, 缺失或落后于最近已完成交易日时受控 auto 更新, 会话内 300s 去抖; 手动刷新走 force）; 失败保留旧结果并提示; 名单变更/诊断运行经 `DuckDBManager` 受控写入口
  - **双模块入口 `dashboard/app.py`**: 侧边栏"账户与信号（默认, 原逻辑拆为 `render_account_panel`）/ 单标的诊断（懒加载）"
  - **名单服务**: `diagnosis_service.watchlist_add/remove/update_note`（身份快照/幂等/软删除/不写策略池）
  - **验证**: 新增 27 测试（数据层 16 / UI-AppTest 7 含快照驱动详情渲染 / 名单服务 4）→ 全量 **611/611**; UI 数据链路冒烟（`rehearsal_m5.duckdb`）: 观察名单增/幂等/移除; 510300 → 23.9 正常, 513100 → 不可评估 且排序在前; K 线全史 1879 → 显示 126 行且首行 MA200 已预热; headless 启动 `_stcore/health=ok`、首页 200; 交易表零变化
  - **未实施（登记）**: P11-M5-08 外部诊断推送（默认关闭, 接线点=trigger 变化 + 既有 notifier 通道）
- **影响范围**: `dashboard/diagnostics_data.py`、`dashboard/diagnostics_app.py`（新）; `dashboard/app.py`（双模块）; `diagnosis_service.py`（名单服务）; `pyproject.toml`/`uv.lock`（plotly）; `docs/user_guide.md`、`README.md`; 3 个测试文件
- **后续动作**: **P11-M4**（股票财务/估值 + ETF 资料费率检查, 按 M1 能力表; 跟踪质量/底层估值映射维持暂缓）→ **P11-M6**（P11-A1–A10 验收对照、在线演示、交接报告、文档同步）

### EV-033 — P11-M4：条件基本面与 ETF 产品检查 🔴

- **日期**: 2026-09-29
- **类型**: 阶段批次完成（条件检查）/ 接口加固 / bug 修复
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段）
- **描述**: P11-M4 完成（按 M1 能力表"可用→实现；暂缓→登记"）:
  - **数据层 `src/data_pipeline/fundamental_data.py`**: 股票三大报表 + 财务指标(扣非) + `stock_value_em` 估值 → 按报告期合并的紧凑 extract（公告日期/币种保留; 'None'/NaN → None）; ETF `fund_overview_em` 资料/费率/规模解析（"948.72亿元（截止至：…）" 形式）+ `fund_etf_fund_daily_em` 净值/折价 + `fund_etf_category_sina` 买卖价快照解析; 全表快照进程内 TTL 缓存 1h
  - **规则卡 `config/condition_rules.yaml`**（cond-v0.1-research, 冻结 2026-09-29, 与评分卡分离版本）
  - **规则层 `src/strategies/condition_checks.py`**: 状态词表 未触发/关注/重点复核/无法评估/不适用; 股票 9 项（营收/归母/扣非 TTM、现金流质量与多期、负债率 70%/85%、短期偿债覆盖（短期借款+一年内到期近似）、净资产、PE/PB 及自身 5 年分位）; ETF 8 项（资料/规模/费率/成交额/快照价差/折溢价/跟踪质量暂缓/底层估值暂缓）; 纪律: 基期非正不给增长率、亏损 PE"不解释为便宜"、金融企业（收入表含"净利息收入"识别）偿债不适用、折溢价缺净值 unknown 且注明不解除禁买
  - **服务/UI 接入**: `run_diagnosis` 组装 `checks.conditions` 入快照（失败不阻塞技术诊断——测试覆盖）; 详情页新增"条件检查"表格（与技术分单列）+ 暂缓项显式展示
  - **接口加固（实测驱动）**: `stock_info_a_code_name` 依赖深交所站点（实测 `www.szse.cn` ConnectTimeout）→ 新增 `stock_zh_a_spot`（新浪全量快照, 含北交所）兜底; `_load_one` 名录失败归一 `SecurityFetchError`（不再泄漏原始堆栈）; 修复 `parse_float` pandas NaN/Inf 泄漏（实测 SV-02 从 "nan 亿 = nan" 修正为"0.57 亿, 覆盖 941 倍"）
  - **验证**: 新增 51 测试（解析 18 / 规则 28 / 服务 3 / securities 兜底净增 3 减去调整）→ 全量 **662/662**; 真实数据副本冒烟（`rehearsal_m4.duckdb`）: 600519 十项全未触发（营收 922.78 亿 +1.3%、归母 445.17 亿 -2.0%、TTM 814.34 亿/扣非 813.67 亿、OCF/净利 1.59、负债率 15.2%、PE 18.97 分位 2%）; 510300 六项未触发（跟踪沪深300/规模 948.72 亿/费率 0.20%/日均成交 31.72 亿/折溢价 +0.05%）; **513100 → 费率 0.80% 关注 + 折溢价无法评估**（跨境当日净值缺失, 不解除禁买）; 交易表零变化
- **影响范围**: `fundamental_data.py`、`condition_checks.py`（新）; `config/condition_rules.yaml`（新, 冻结）; `securities.py`（名录兜底/错误归一）; `diagnosis_service.py`（conditions 装配）; `dashboard/diagnostics_app.py`（条件检查区块）; `docs/user_guide.md`; 4 个测试文件
- **后续动作**: **P11-M6**（P11-A1–A10 验收对照、在线端到端演示、交接报告、文档同步; 不做新功能扩展）

### EV-034 — P11-M6 验收交接 → **Phase 1.1 收官** 🎉🔴

- **日期**: 2026-09-29
- **类型**: 阶段批次完成（验收/交接）/ Phase 里程碑 / bug 修复
- **与 task.md 关系**: 互补（Phase 1.1 新增阶段收官）
- **描述**: P11-M6 完成，Phase 1.1（P11-M0–M6）全部收官:
  - **验收对照（P11-A1–A10）**: 全部 ✅（task-phase1.1 §11.1, 逐项引用测试名/命令/运行记录; 无受阻项）
  - **在线端到端演示（真实库, 6 类样本; 证据 `data/backups/m6_e2e_demo_20260929_2246.json`）**: 510300 完整 ETF（23.9 正常, 条件未触发 6）; 513100 跨境（事件暂停不可评估, 折溢价 unknown 不解除禁买）; 301696 短历史股票 118 根（不可评估 1/5, 不伪造）; 600519（37.7 警戒）; 601398 银行（0.0 正常; 偿债不适用; PE 分位 98% 关注）; 999999 错误代码（明确报错）。观察名单加入→再次打开可见; 交易表零变化。耗时：冷启动≈45.7s（含 szse 超时→sina 兜底 ≈20s）/热缓存 0.3–4.7s
  - **回归（P11-M6-02）**: 全量 **663/663**; `rehearsal_m7.py` 在 v8 库上 ALL_OK（迁移 versions[1..8]/回滚/重跑/恢复）; `uv run backtest` 偏差 0.043%/0.031% + T+0/T+1 交收差一天
  - **修复**: 银行权益字段名差异（"归属于母公司股东的权益" 回退; 601398 SV-03 恢复"未触发"并刷新真实快照）; 观察名单空态测试改为隔离临时库（不再依赖真实库可变状态）
  - **文档同步**: `task-phase1.1.md`（M6 状态 + **§11 验收与交接报告**）; `HANDOFF.md`（重写为 Phase 1.1 收官交接）; `README.md`（663）; `user_guide.md`（此前已更新）; AGENTS 评估=无需修订（§11.8）
- **影响范围**: `docs/task-phase1.1.md`、`HANDOFF.md`、`README.md`、`docs/PROJECT_MEMORY.md`; `fundamental_data.py`（银行字段）; `tests/test_fundamental_data.py`、`tests/test_diagnostics_app.py`; 真实库新增演示数据（5 快照 / 2 观察 / securities 缓存）
- **后续动作**: **Phase 1 研究（P1-R1–R6, 全部未开始, 需用户确认）**: 先 P1-R1（I-09 方案2 权益入账 + A 回测风控对齐）→ P1-R2/R3 因子与 RankIC/ICIR → R4/R5 → R6（先做 I-02 决策）。Phase 1.1 完成 ≠ 实盘授权（真实账户启用 Gate 未触发）

---

## 四、关键产出索引

| 产出                                 | 路径                                              | Phase | 状态                         |
| :----------------------------------- | :------------------------------------------------ | :---- | :--------------------------- |
| 项目配置 + 依赖管理                  | `pyproject.toml`                                | P0    | ✅                           |
| 全局参数 + 20 ETF 标的池             | `config/settings.yaml`                          | P0    | ✅                           |
| 因子注册表 (空)                      | `config/factor_registry.yaml`                   | P0    | ✅ 待 P1-D1 填充             |
| 任务路线图                           | `docs/task.md`                                  | P0    | ✅ T0.1 已完成               |
| Akshare 双源获取 (Sina + eastmoney)  | `src/data_pipeline/fetchers/akshare_fetcher.py` | P0    | ✅ Sina 主力, eastmoney 备用 |
| DuckDB 数据库 (19 ETFs, 34,073 rows) | `data/quant_pilot.duckdb`                       | P0    | ✅ 全量历史数据已入库        |
| DuckDB 管理器 (6 表 + UPSERT)        | `src/data_pipeline/duckdb_manager.py`           | P0    | ✅                           |
| DuckDB 单元测试 (14 tests)           | `tests/test_duckdb_manager.py`                  | P0    | ✅ 全部通过                  |
| 数据质量护栏 (5 项检查)              | `src/data_pipeline/processors/validator.py`     | P0    | ✅                           |
| 宏观择时模块 (维度①②)              | `src/strategies/macro_timing.py`                | P0    | ✅                           |
| 择时单元测试 (20 tests)              | `tests/test_macro_timing.py`                    | P0    | ✅ 全部通过                  |
| 标的清洗模块 (4 项筛选)              | `src/strategies/universe_filter.py`             | P0    | ✅                           |
| 标的清洗测试 (39 tests)              | `tests/test_universe_filter.py`                 | P0    | ✅ 全部通过                  |
| 组合优化模块 (逆方差加权)            | `src/strategies/portfolio_optimizer.py`         | P0    | ✅                           |
| 组合优化测试 (43 tests)              | `tests/test_portfolio_optimizer.py`             | P0    | ✅ 全部通过                  |
| 风控模块 (ATR + 骤降)               | `src/strategies/risk_manager.py`                | P0    | ✅                           |
| 风控测试 (41 tests)                  | `tests/test_risk_manager.py`                    | P0    | ✅ 全部通过                  |
| 每日主流程 (五层漏斗串联)            | `src/orchestration/daily_pipeline.py`           | P0    | ✅ 🆕                        |
| 主流程集成测试 (6 tests)             | `tests/test_daily_pipeline.py`                  | P0    | ✅ 全部通过 🆕               |
| 消息推送模块 (PushPlus)              | `src/orchestration/notifier.py`                 | P0    | ✅ 真实推送 🆕               |
| 推送单元测试 (22 tests)              | `tests/test_notifier.py`                        | P0    | ✅ 全部通过 🆕               |
| 自研回测引擎 (T+0/T+1 撮合)          | `src/backtest/custom_loop.py`                   | P0    | ✅ 偏差 0.044% 🆕            |
| 回测测试 (20 tests)                  | `tests/test_custom_loop.py`                     | P0    | ✅ 全部通过 🆕               |
| 信号面板数据层                      | `dashboard/data_loader.py`                      | P0    | ✅ 🆕                        |
| Streamlit 信号面板                   | `dashboard/app.py`                              | P0    | ✅ 冒烟通过 🆕               |
| 面板数据层测试 (16 tests)            | `tests/test_dashboard.py`                       | P0    | ✅ 全部通过 🆕               |
| 账户账本 (事件/回放/批次)            | `src/data_pipeline/accounting.py`               | M2    | ✅ 🆕                        |
| 建议/反馈账本 (状态机)               | `src/orchestration/proposal_ledger.py`          | M2    | ✅ 🆕                        |
| 四层池与产品契约                     | `src/strategies/etf_pool.py`, `product_compare.py` | M3 | ✅ 🆕                        |
| 调仓规划器 (目标→建议)               | `src/strategies/rebalance_planner.py`           | M4    | ✅ 🆕                        |
| 持有期状态 (高水位/棘轮/冷却)        | `src/strategies/strategy_states.py`             | M4    | ✅ 🆕                        |
| 调仓编排 (账户→状态→建议落库)        | `src/orchestration/rebalance_run.py`            | M4    | ✅ 🆕                        |
| Phase 1.1 任务/接口调查/验收证据      | `docs/task-phase1.1.md`                         | P1.1  | [~] M0/M1/M2 ✅, M3 待启动    |
| 接口探针 (可复跑, 分组)               | `scripts/probe_phase11_interfaces.py`           | P1.1  | ✅ M1 证据                    |
| 证券身份解析 (名录缓存/搜索)          | `src/data_pipeline/securities.py`               | P1.1  | ✅ M2                         |
| 诊断统一取数 (日历/缓存/事件检查)     | `src/data_pipeline/diag_data.py`                | P1.1  | ✅ M2                         |
| schema v8 迁移演练脚本                | `scripts/rehearsal_v8.py`                       | P1.1  | ✅ M2 证据                    |
| 诊断指标库 (MA/动量/ATR/RSI/布林/量额) | `src/factor_engineering/technical.py`          | P1.1  | ✅ M3                         |
| 诊断规则卡 (研究示例, 冻结)           | `config/diagnostics.yaml`                       | P1.1  | ✅ M3 diag-v0.1-research      |
| 计分引擎与解释模板 (无 LLM)           | `src/strategies/diagnosis.py`                   | P1.1  | ✅ M3                         |
| 统一诊断服务 (状态机/快照)            | `src/orchestration/diagnosis_service.py`        | P1.1  | ✅ M3                         |
| 诊断页面数据层 (K线/列表/持仓)        | `dashboard/diagnostics_data.py`                 | P1.1  | ✅ M5                         |
| 诊断页面渲染 (三入口/详情/K线图)      | `dashboard/diagnostics_app.py`                  | P1.1  | ✅ M5                         |
| 双模块面板入口                        | `dashboard/app.py`（账户与信号 + 单标的诊断） | P1.1  | ✅ M5                         |
| 基本面/ETF 条件数据层                 | `src/data_pipeline/fundamental_data.py`         | P1.1  | ✅ M4                         |
| 条件检查规则层                        | `src/strategies/condition_checks.py`            | P1.1  | ✅ M4                         |
| 条件检查规则卡 (研究示例, 冻结)       | `config/condition_rules.yaml`                   | P1.1  | ✅ M4 cond-v0.1-research      |
| Phase 1.1 验收与交接报告              | `docs/task-phase1.1.md` §11                     | P1.1  | ✅ M6                         |
| 会话交接 (Phase 1.1 收官)             | `HANDOFF.md`                                    | P1.1  | ✅ M6 重写                    |

---

## 五、未解决问题

| #    | 问题描述                                                                       | 发现日期   | 已尝试方案                                             | 需要帮助                                     |
| :--- | :----------------------------------------------------------------------------- | :--------- | :----------------------------------------------------- | :------------------------------------------- |
| I-01 | ~~eastmoney API 间歇性断连~~ → **已解决: 切换至 Sina 数据源** (EV-007) | 2025-07-18 | 反封锁休眠 / 代理绕过 / 指数退避重试 → 最终方案: Sina | 无需帮助                                     |
| I-02 | Python 3.14 与 vectorbt 不兼容 (P1 需要)                                       | 2025-07-18 | vectorbt 移入 optional-deps p1 分组                    | P1 时用`uv python pin 3.12` 或 Docker 隔离 |
| I-03 | NAV/IOPV 未入库 → M3 起**不再静默**: 跨境产品禁新买（真实干跑 14/19 可买）；解锁需接入 NAV 数据 | 2025-07-18 | M3 四层池实现 unknown→禁买（保守默认）；`nav_daily` 表已就绪 | 专项接入 NAV 数据源（P1 或提前申请） |
| I-04 | 复权口径: ~~AGENTS §5.2 "前复权" 条款待修订~~ → ✅ AGENTS 条款已随 v3.0 修订（2026-09-14）; Sina 数据 DIY 后复权/权益事件并入 I-09 (M2/M3) 与 P1 | 2025-07-19 | eastmoney 备用路径已改 hfq; AGENTS v3.0 | M2 权益事件 / P1 复权序列实现 |
| I-05 | 组合优化 Top-N 截断致仓位利用率低 (设计内行为, EV-011)                          | 2025-07-24 | 记录观察, P0 不改动                                    | P1 风险平价 (T1.7) 时评估                   |
| I-06 | spec v3.0 §9 审计 A 类: C01/C04/C08 (M1-1) + C05/C06/C09/C10/C11 (M1-2) ✅ 全部修正; C03 部分 (M1-3) ✅ | 2026-09-14 | M1 完成, 280/280 | 无 (完整四类记录/池/再平衡随 M2–M6) |
| I-07 | ~~AGENTS.md §5.2/§7 旧条款与 spec v3.0 冲突~~ → ✅ **已修订至 v3.0**（用户 2026-09-14 确认） | 2026-09-14 | M1-3 完成 | 无 |
| I-08 | ~~固定 \|涨跌\|>20% 阈值可能误伤 20% 板真实极端日~~ → ✅ 已解决 (M1-2 产品感知分级: 10%/20% 板 + 2pp 容差) | 2026-09-14 | M1-2-01 完成 | 无 |
| I-09 | 历史行情含未调整权益事件跳变（拆分/折算 10+ 处）+ 2026-07-19 全量 fetch-data 绕过校验写入 → **M8（2026-09-20）已建权益事件登记表（schema v7, 12 条）与拆分感知质检**: 登记命中→不阻断, 未登记→critical; 方案2（账户入账）登记至 Phase 1 | 2026-09-14 | M1-2 产品感知质检 + 口径标注; M8 登记表/质检/脚本; 不自动改数据 | 方案2（官方分红/拆分数据接入 + 账户入账）在 Phase 1; 512100 因子待核验; ⚠️ 增量遇未登记拆分日仍阻断, 人工核验后登记, **勿用全量 fetch-data 绕过** |

---

## 六、下次会话起点

1. ~~T0.1: DuckDB 数据初始化~~ (已完成)
2. ~~T0.2: 数据质量护栏~~ (已完成)
3. ~~T0.3: 宏观择时模块~~ (已完成)
4. ~~T0.4: 标的清洗模块~~ (已完成)
5. ~~数据源修复: Sina 迁移 + 全量导入~~ (已完成 — EV-007, EV-008)
6. ~~T0.5: 组合优化模块~~ (已完成)
7. ~~T0.6: 风控模块~~ (已完成)
8. ~~T0.7: 每日盘后主流程编排~~ (已完成 — EV-009; `uv run daily` 真实冒烟通过, 2.1s 全流程, 信号幂等落库)
9. ~~T0.8: 消息推送模块~~ (已完成 — EV-012; 2026-09-11 真实推送 PushPlus code=200, 22 tests)
10. ~~T0.9: 自研回测循环 v0.1~~ (已完成 — EV-013; 偏差 0.044% < 1%, 交收差一天 ✓, 20 tests)
11. ~~T0.10: Streamlit 信号面板~~ (已完成 — EV-014; AppTest 冒烟 + headless 服务验证, 16 tests)
12. **🎉 P0 Phase 完成 (旧口径)** (2026-09-13): 交付物 10/10, Gate 6/6
13. **当前: Phase 0.5 + M8 ✅ 完成（权益事件登记/拆分感知质检）** (2026-09-20)
    - M1–M8 交付: A 类纠偏 + Phase 0 收尾; 账户/四类记录; 四层池; 调仓规划/状态化止损; B0/B1 基准; 绩效/面板; 演练验收; 权益事件登记与拆分感知质检
    - 关键产出: 447/447 测试; `data/backups/` pre_v2→v7 快照 + 演练报告; `equity_events` 12 条登记（推断值待核验）
    - **下一步由用户确认**: Phase 1（含 I-09 方案2 权益事件入账、I-02 Python 版本决策、少量因子验证）; 不自动进入
    - 纪律: 真实账户启用 Gate 未触发; Phase 0.5 完成 ≠ 允许实盘
14. ✅ 全量测试: 447/447 通过 (2026-09-20)
15. 📋 常用运行命令见 §七 (面板/取数/回测/测试)
16. 参考: `docs/task-phase0.5.md`; `docs/spec.md` v3.0 §8/§9; `docs/plan.md` M1–M7
17. **Phase 1.1 完成 — P11-M0–M6 全部 ✅（2026-09-29 收官）**
    - 任务文件 `docs/task-phase1.1.md`（用户已确认）; **§11 验收与交接报告**（P11-A1–A10 全过 / 在线演示 / P11-D1–D9 / 支持矩阵 / P1-R1–R6 独立状态）; 交接见 `HANDOFF.md`
    - M1 完成: 36 项接口实测（探针 `scripts/probe_phase11_interfaces.py`; JSON 证据 `data/backups/probe_phase11_20260928_*.json`）; 附录 A 能力表固化; 暂缓项 = ETF 跟踪质量、底层估值映射（明确记录）
    - M2 完成: schema v8 五表 + `securities.py` 身份解析 + `diag_data.py` 统一取数/日历/事件检查; `rehearsal_v8` 副本演练 + 真实库应用; 股票口径 hfq（全量替换）
    - M3 完成: 指标库 `technical.py` + 规则卡冻结（diag-v0.1-research）+ 注册段 D01–D12 + 计分/解释 + 快照状态机 + `diagnosis_service.py`; `price_version` 修正为口径版本
    - M5 完成: plotly 6.9.0 入主依赖（D1）; 面板双模块（账户与信号默认 + 单标的诊断）; 三入口 + 六个月日K（全史预热）+ 刷新冷却去抖/手动刷新; 名单服务; headless health=ok; `QUANT_PILOT_DB` 可指向演练副本
    - M4 完成: 条件规则卡冻结（cond-v0.1-research）+ `fundamental_data.py` + `condition_checks.py`（股票 9 项 / ETF 8 项; 单列展示不混技术分）; 名录新浪快照兜底 + NaN 修复
    - M6 完成: 验收对照全过; 在线端到端演示（6 类样本, 证据 `m6_e2e_demo_20260929_2246.json`）; 回归 **663/663** + `rehearsal_m7` ALL_OK + `backtest` 偏差 0.043%/0.031%; 银行权益字段修复
    - 下一步: **Phase 1 研究（P1-R1–R6, 全部未开始, 需用户确认）**: 先 P1-R1（I-09 方案2 权益入账 + A 回测风控对齐）→ P1-R2/R3 因子与 RankIC/ICIR → R4/R5 → R6（先做 I-02 决策）
    - 纪律: 诊断链不写账户/目标/建议/池; 未核验权益事件窗口暂停相关指标; 规则卡冻结后调参走新 rules_version; **Phase 1.1 完成 ≠ 实盘授权（真实账户启用 Gate 未触发）**

---

## 七、常用运行命令

```powershell
# 全量取数 (空库初始化 / 重建历史, 19 ETFs 全历史)
uv run fetch-data

# 每日增量取数 (自动回溯 10 天补拉) + 五层漏斗 + 信号落库 + 手机推送
uv run daily
#   真实推送需先设置环境变量 (EV-012):
#   PowerShell: $env:PUSHPLUS_TOKEN="<token>"; uv run daily
#   Git Bash:   PUSHPLUS_TOKEN=<token> uv run daily

# 回测 (510300 买入持有偏差验收 + T+0/T+1 交收对比)
uv run backtest

# 账户事件导入 (标准 CSV; 重复导入自动去重; 模板 docs/account_events_template.csv)
uv run account-import --account REAL-1 --name "项目账户" --mode real --csv events.csv

# 基准模拟 (M5; B0 买入持有 / B1 阈值再平衡; 研究示例配置; 结果写 data/backtests/)
uv run baseline --strategy B0 --start 2019-01-02 --end 2026-09-11
uv run baseline --strategy B1 --write-account     # 同时把模拟账户写入数据库

# Streamlit 信号面板 (默认 http://localhost:8501)
uv run streamlit run dashboard/app.py

# 全量测试 (447)
uv run pytest tests/ -v

# 权益事件登记 (M8; --check 列出未登记巨幅跳变; --apply 登记已核验清单)
uv run python scripts/register_equity_events.py --check
uv run python scripts/register_equity_events.py --apply
```
