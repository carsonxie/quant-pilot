# PROJECT_MEMORY.md — Quant-Pilot 项目持久记忆

> **定位**: 项目的"外脑"。记录实际发生的事实、新增约束、设计变更。与 `task.md`（原始计划，只读）互补。
>
> **更新规则**: 演化记录只追加不删除。每次 Phase 完成或关键决策做出后必须更新。详细更新指令见 `prompts/update_memory_prompt.md`。

---

## 一、项目状态快照

| 字段                   | 值                                                                  |
| :--------------------- | :------------------------------------------------------------------ |
| **项目启动日期** | 2025-07-15                                                          |
| **当前 Phase**   | Phase 0.5 — **M5 完成**（B0/B1 基准 + 共用执行基础）→ M6 待启动 |
| **当前 Task**    | `docs/task-phase0.5.md`（M1 ✅；M2 ✅；M3 ✅；M4 ✅；M5 ✅；M6 待启动） |
| **Phase 进度**   | Phase 0 代码 T0.1~T0.10 ✅ / §8.1 收尾补项 6 组 ✅（A0-1…A0-6 全部闭合；推送失败持久化转 M2） |
| **Gate 状态**    | 旧口径 6/6 ✅；spec §8.7 三 Gate 体系下 Phase 0 收尾证据已闭合（`docs/phase0.5-audit.md` §1 全绿）                                 |
| **最后更新**     | 2026-09-15 (M5 完成: B0/B1 基准/实验记录/同口径, 411/411 测试, M6 待启动) |

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
| I-09 | 历史行情含未调整权益事件跳变 (拆分/折算 10+ 处, 见 EV-017) + 2026-07-19 全量 fetch-data 绕过校验写入 | 2026-09-14 | M1-2 产品感知质检 + 口径标注; 不自动改数据 | M2 权益事件入账 / M3 数据版本; ⚠️ 增量遇拆分日会阻断, 须人工核验, **勿用全量 fetch-data 绕过** |

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
13. **当前: Phase 0.5 — M5 ✅ 完成（B0/B1 基准 + 实验记录 + 同口径）, 待确认启动 M6** (2026-09-15)
    - M1–M5 交付: A 类纠偏 + Phase 0 收尾 + AGENTS v3.0; 账户/四类记录/schema v3; 四层池/schema v4; 调仓规划/状态化止损/schema v5; B0/B1 基准与实验记录
    - 关键产出: 411/411 测试; `data/backups/` pre_v2→v5 快照与演练副本; `uv run baseline` CLI; 基准真实数据结果（B0 +104.72%/−31.04%, B1 +102.16%/−29.84%, 研究示例配置）
    - 下一步 M6: 基础绩效（期末−期初−净入金=投资盈亏）、入金不计收益、贡献与差额单列、面板扩展（实际/目标、待处理建议、B0/B1/A 净值对照）
    - 纪律: 每批"实现→验证→更新审计/任务→停靠汇报", 待用户确认后进入下一节
14. ✅ 全量测试: 411/411 通过 (2026-09-15)
15. 📋 常用运行命令见 §七 (面板/取数/回测/测试)
16. 参考: `docs/task-phase0.5.md`; `docs/spec.md` v3.0 §8/§9; `docs/plan.md` M1–M7

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

# 全量测试 (411)
uv run pytest tests/ -v
```
