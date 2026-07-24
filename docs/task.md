# task.md — Quant-Pilot 项目路线图

> **定位**: 项目原始计划，只读。严格基于 `spec.md §7 Phase Plan` 创建。
> **状态标记**: `- [ ]` 未开始 / `- [~]` 进行中 / `- [x]` 已完成 / `- [!]` 受阻

---

## Phase 0: MVP 闭环 (Minimal Viable Pipeline) — 预计耗时: 6-8 周

### 目标
让系统每天盘后自动输出一条有理有据、可执行、可追溯的交易建议。打通"数据拉取 → 因子计算 → 信号生成 → 消息推送"全流程。

### 前置依赖
- 无

### 任务清单

- [x] **T0.1** — 数据层初始化 — 预计: 8-10h (实际: ~4h)
  - 产出: `src/data_pipeline/duckdb_manager.py`、`src/data_pipeline/fetchers/akshare_fetcher.py`、`config/settings.yaml`、`pyproject.toml`、`tests/test_duckdb_manager.py` (14 tests)
  - 内容: DuckDB 6 表 schema、Akshare 历史数据获取（20 ETFs）、幂等 UPSERT、反封锁随机休眠 0.5-3s、代理绕过、指数退避重试
  - 验收: 14/14 单元测试通过；幂等性验证通过；smoke test 真实拉取 510300 成功 (131 rows)；⚠️ eastmoney API 偶发断连, 网络恢复后运行 `uv run fetch-data` 即可全量导入

- [x] **T0.2** — 数据质量护栏 — 预计: 4-6h (实际: ~2h)
  - 产出: `src/data_pipeline/processors/validator.py`、`tests/test_validator.py` (21 tests)
  - 内容: 5 项质检 — 负价格拦截/涨跌幅 >20% 阻断/缺失值比例报警/时间戳连续性/行数偏离监控; `DataQualityError` 异常 + `QualityIssue` 数据结构
  - 验收: 21/21 测试通过; 手动注入单日涨跌幅 25% 脏数据 → 阻断; 手动注入负价格 → 阻断

- [x] **T0.3** — 宏观择时模块（维度①②） — 预计: 6-8h (实际: ~2h)
  - 产出: `src/strategies/macro_timing.py`、`tests/test_macro_timing.py` (20 tests)
  - 内容: 趋势动量打分 (MA20/MA60 排列 + 价格偏离 + 斜率)、市场宽度打分 (站上 MA20 比例)、0-100 复合得分、分段线性仓位映射、单日骤降检测 (plunge_threshold=30)
  - 验收: 20/20 测试通过; 牛市得分 >60; 熊市得分 <40; 得分 ≤20 → 空仓; 骤降检测正确

- [x] **T0.4** — 标的清洗模块 — 预计: 4-6h (实际: ~2h)
  - 产出: `src/strategies/universe_filter.py`、`tests/test_universe_filter.py` (39 tests)
  - 内容: 4 项筛选 — 日均成交额 < 500 万剔除/溢价 > 3% 剔除/上市 < 60 天剔除/流动性集中度 > 0.5% 警告; `UniverseFilterResult` 数据结构 + `from_settings()` 工厂函数
  - 依赖: T0.1
  - 验收: 39/39 单元测试通过; 给定多标的快照正确分类通过/剔除/警告; 溢价检查支持 NAV/IOPV 两种列名, 无 NAV 数据时静默跳过

- [x] **T0.5** — 波动率倒数加权组合优化 — 预计: 3-4h (实际: ~2h)
  - 产出: `src/strategies/portfolio_optimizer.py`, `tests/test_portfolio_optimizer.py` (43 tests)
  - 内容: 基于过去 20 日年化波动率计算权重 `w_i ∝ 1/σ_i²`，归一化到总仓位上限；单标的 ≤30% 迭代裁剪；NaN 防御；最多 5 只持仓截断
  - 依赖: T0.3（需要仓位上限）
  - 验收: 43/43 测试通过; 低波动标的获更高权重; 迭代裁剪正确分配; 权重和 ≤ 仓位上限

- [x] **T0.6** — ATR 动态止损 + 宏观骤降强制减仓 — 预计: 5-7h (实际: ~1.5h)
  - 产出: `src/strategies/risk_manager.py`, `tests/test_risk_manager.py` (41 tests)
  - 内容: ATR(14) Wilder 平滑计算; 移动最高价追踪; 止损线 = 最高价 - N×ATR, 只上移不下移 (enforce_stop_ratchet); 宏观得分单日下降 > 30 → 强制减仓至 safe_position_pct
  - 依赖: T0.3（需要宏观得分）
  - 验收: 41/41 测试通过; ATR Wilder 平滑正确; 上涨行情不触发止损; 暴跌触发止损; 止损棘轮只上移; 骤降仓位降至 30%

- [ ] **T0.7** — 每日盘后主流程编排 — 预计: 5-7h
  - 产出: `src/orchestration/daily_pipeline.py`
  - 内容: 串联数据拉取 → 数据质检 → 宏观择时 → 标的清洗 → 组合优化 → 风控检查 → 生成信号，每步记录日志和耗时
  - 依赖: T0.2, T0.3, T0.4, T0.5, T0.6
  - 验收: 在任一交易日盘后运行 `daily_pipeline.py`，从拉取到信号生成全流程无报错；日志记录完整

- [ ] **T0.8** — 消息推送模块 — 预计: 3-4h
  - 产出: `src/orchestration/notifier.py`
  - 内容: 至少支持一种推送通道（PushPlus/飞书/邮件）；推送内容包含：标的、方向、目标仓位、触发原因（多层级归因）；推送失败重试 3 次
  - 依赖: T0.7
  - 验收: 运行 pipeline 后手机收到信号推送；推送内容包含触发原因

- [ ] **T0.9** — 自研轻量回测循环 v0.1 — 预计: 8-10h
  - 产出: `src/backtest/custom_loop.py`
  - 内容: 标的元数据驱动撮合（T+0/T+1 区分）、动态仓位上限、T+1 开盘价成交、双边佣金 ≥ 0.15% + 滑点（max(0.15%, 交易额/日均成交额 × 0.5%)）、每日净值记录
  - 依赖: T0.5, T0.6（需要组合优化和风控模块的接口）
  - 验收: 对沪深300 ETF 单标的买入持有回测，净值曲线与指数实际走势年度收益率偏差 < 1%；分别对 T+1 ETF（510300）和 T+0 ETF（513100）回测，验证交收日期差一天

- [ ] **T0.10** — Streamlit 单页面信号面板 — 预计: 4-6h
  - 产出: `dashboard/app.py`
  - 内容: 展示今日信号、触发原因、当前持仓表格、近期净值曲线图；从 DuckDB 读取最新信号和持仓数据
  - 依赖: T0.7, T0.8
  - 验收: 本地 `streamlit run` 后可看到信号面板；信号与 DuckDB 中记录一致

### 交付物

- [ ] `src/data_pipeline/duckdb_manager.py` — DuckDB 交互层
- [ ] `src/data_pipeline/fetchers/akshare_fetcher.py` — Akshare 数据获取
- [ ] `src/data_pipeline/processors/cleaner.py` — 数据清洗与质量护栏
- [ ] `src/data_pipeline/processors/validator.py` — Schema 与时间戳校验
- [ ] `src/strategies/macro_timing.py` — 宏观择时（维度①②）
- [ ] `src/strategies/universe_filter.py` — 标的清洗
- [ ] `src/strategies/portfolio_optimizer.py` — 波动率倒数加权
- [ ] `src/strategies/risk_manager.py` — ATR 止损 + 强制减仓
- [ ] `src/orchestration/daily_pipeline.py` — 每日主流程
- [ ] `src/orchestration/notifier.py` — 消息推送
- [ ] `src/backtest/custom_loop.py` — 自研回测循环
- [ ] `dashboard/app.py` — Streamlit 信号面板

### 验收标准 (Gate Criteria)

- [ ] **闭环验收**: 在任一交易日盘后运行 `daily_pipeline.py`，从 Akshare 拉取 → 宏观打分 → 信号生成 → 推送到手机，全流程无报错
- [ ] **回测正确性**: 自研回测循环对沪深300 ETF 单标的买入持有，净值曲线与指数实际走势年度收益率偏差 < 1%
- [ ] **T+0/T+1 正确性**: 分别对 T+1 ETF（510300）和 T+0 ETF（513100）回测，交收日期差一天
- [ ] **数据护栏有效**: 手动注入单日涨跌幅 25% 脏数据 → pipeline 阻断并报警
- [ ] **信号可追溯**: 任意信号推送中，用户能读到"因为什么触发、建议做什么、仓位多少"
- [ ] **无未来函数**: `tests/` 中至少一个测试用例验证 T 日计算不依赖 T+1 及以后数据

---

## Phase 1: 因子增强与回测验证 (Factor Enhancement & Rigorous Backtest) — 预计耗时: 8-10 周

### 目标
截面多因子打分体系建立，并通过严苛的样本外回测验证策略的统计有效性。

### 前置依赖
- P0 全部验收通过

### 任务清单

- [ ] **T1.1** — 因子注册表初始化 + 因子计算模块 — 预计: 10-12h
  - 产出: `config/factor_registry.yaml`（≥ 8 个因子）、`src/factor_engineering/technical.py`、`src/factor_engineering/fundamental.py`、`src/factor_engineering/macro.py`
  - 内容: 初始化因子注册表（动量、波动、成交量、估值等 ≥ 8 个因子），每个因子附带数学公式与代码路径；实现对应计算函数
  - 验收: `factor_registry.yaml` 包含 ≥ 8 个因子；每个因子的 `code_path` 可导入并返回正确形状的 DataFrame

- [ ] **T1.2** — CI 因子校验脚本 — 预计: 3-4h
  - 产出: `scripts/validate_registry.py`
  - 内容: 验证 YAML 中注册的每个因子都有对应可导入函数；验证 `factor_engineering/` 下每个因子函数都在 YAML 注册；验证必填字段完整
  - 依赖: T1.1
  - 验收: `uv run factor-check` 无报错，双向一致性通过

- [ ] **T1.3** — 因子预处理管道 — 预计: 5-7h
  - 产出: `src/factor_engineering/preprocessor.py`
  - 内容: MAD 去极值（threshold=3.0）、Z-Score 标准化、行业/市值中性化（线性回归残差法）
  - 依赖: T1.1
  - 验收: 去极值后因子最大值不超过 MAD 阈值；中性化后因子与行业/市值的相关系数 < 0.01

- [ ] **T1.4** — 截面多因子加权打分 + 换手率惩罚 — 预计: 6-8h
  - 产出: `src/strategies/cross_sectional.py`
  - 内容: 因子加权合成截面得分，选出 Top 3-5 只标的；换手率惩罚（新旧仓位差异 < 15% 不调仓、最低持仓 3 天）
  - 依赖: T1.3
  - 验收: 给定因子矩阵，输出排名正确的标的清单；换手率惩罚在 A/B 测试中降低换手率 > 30%

- [ ] **T1.5** — Alphalens 因子评估报告 — 预计: 4-6h
  - 产出: `src/factor_engineering/evaluator.py`
  - 内容: 封装 alphalens-reloaded，对每个因子输出 IC 均值/IR/分位数收益图
  - 依赖: T1.1
  - 验收: 每个因子都有 IC 均值（绝对值）> 0.02 或 IR > 0.3；分位数收益图呈明显单调性

- [ ] **T1.6** — 宏观择时维度③④⑤接入 — 预计: 6-8h
  - 产出: `src/strategies/macro_timing.py`（升级）
  - 内容: 按顺序逐步接入：③波动率 → ④资金流向 → ⑤信用利差；每个维度经验证后上线
  - 依赖: T0.3
  - 验收: 每个新维度接入后，宏观得分在对应市场环境中有合理响应

- [ ] **T1.7** — 组合优化升级为风险平价 — 预计: 8-10h
  - 产出: `src/strategies/portfolio_optimizer.py`（升级）
  - 内容: 实现风险平价 (Risk Parity) + Ledoit-Wolf 协方差收缩；边际风险贡献 (MRC) 计算
  - 依赖: T0.5
  - 验收: MRC 均等化误差 < 5%；Ledoit-Wolf 收缩后样本外风险预测误差低于样本协方差

- [ ] **T1.8** — 完整回测报告 — 预计: 8-10h
  - 产出: `src/backtest/analytics.py`（含 Brinson 归因）
  - 内容: 2015-2021 样本内训练 + 2022-2024 纯样本外测试；输出完整绩效指标（年化收益、夏普、最大回撤、Calmar、胜率、盈亏比）
  - 依赖: T1.4, T1.6, T1.7
  - 验收: 2022-2024 样本外年化收益率 ≥ 8%，夏普 ≥ 0.8，最大回撤 < 20%

- [ ] **T1.9** — vectorbt 适配器封装 — 预计: 4-6h
  - 产出: `src/backtest/vectorbt_adapter.py`
  - 内容: 封装 vectorbt 用于快速对比验证自研引擎结果
  - 依赖: T0.9
  - 验收: 同一策略在自研引擎与 vectorbt 上日收益率序列相关系数 > 0.9999

- [ ] **T1.10** — 绩效归因报告 — 预计: 4-6h
  - 产出: `src/backtest/analytics.py`（Brinson 分解）
  - 内容: 将收益拆解为择时贡献 / 选股贡献 / 交互效应
  - 依赖: T1.8
  - 验收: 归因报告三项贡献之和 = 总超额收益（闭合）

### 交付物

- [ ] `config/factor_registry.yaml` — ≥ 8 个因子注册
- [ ] `src/factor_engineering/technical.py` — 量价因子
- [ ] `src/factor_engineering/fundamental.py` — 基本面因子
- [ ] `src/factor_engineering/macro.py` — 宏观因子
- [ ] `src/factor_engineering/preprocessor.py` — 预处理管道
- [ ] `src/factor_engineering/evaluator.py` — Alphalens 封装
- [ ] `src/strategies/cross_sectional.py` — 截面多因子打分
- [ ] `scripts/validate_registry.py` — CI 校验脚本
- [ ] `src/backtest/vectorbt_adapter.py` — vectorbt 封装
- [ ] `src/backtest/analytics.py` — 绩效归因

### 验收标准 (Gate Criteria)

- [ ] **样本外回测达标**: 2022-2024 样本外年化 ≥ 8%，夏普 ≥ 0.8，最大回撤 < 20%
- [ ] **参数稳定性**: 核心参数 ±20% 邻域内，样本外夏普变化 < 30%
- [ ] **因子质量**: 每个因子 IC 均值（绝对值）> 0.02 或 IR > 0.3，分位数单调
- [ ] **震荡市识别**: 标注震荡区间内信号频率相比趋势市下降 > 50%
- [ ] **回测引擎一致性**: 自研引擎 vs vectorbt 日收益率相关系数 > 0.9999
- [ ] **因子注册表 CI 通过**: `uv run factor-check` 无报错

---

## Phase 2: ML/DL 模型引入 (Machine Learning Integration) — 预计耗时: 8-12 周

### 目标
用 LightGBM/XGBoost 替代或辅助线性因子打分，并建立防过拟合的实验追踪体系。

### 前置依赖
- P1 全部验收通过

### 任务清单

- [ ] **T2.1** — LightGBM 分类器与回归器封装 — 预计: 8-10h
  - 产出: `src/models/ml_predictors.py`
  - 内容: 分类器（预测未来 N 日胜率）+ 回归器（预测未来 N 日收益率）；特征工程自动化；早停机制
  - 验收: 模型训练完成，输出样本外预测结果

- [ ] **T2.2** — Purged K-Fold 交叉验证 — 预计: 5-7h
  - 产出: `src/models/ml_predictors.py`（升级）
  - 内容: 实现 Purged K-Fold（含 Embargo 隔离带），替换标准 K-Fold
  - 依赖: T2.1
  - 验收: 验证集 IC 与样本外 IC 差异 < 30%

- [ ] **T2.3** — 轻量实验追踪表 — 预计: 4-6h
  - 产出: `src/data_pipeline/duckdb_manager.py`（升级，新增 experiment 表）
  - 内容: 基于 DuckDB 的实验记录表（超参数、训练/验证集划分、IC 衰减曲线）
  - 依赖: T2.1
  - 验收: 两次独立运行相同超参数产出相同结果（bitwise deterministic）

- [ ] **T2.4** — 特征重要性与 SHAP 报告 — 预计: 4-6h
  - 产出: `src/models/ml_predictors.py`（升级）
  - 内容: 特征重要性分析 + SHAP 可解释性报告
  - 依赖: T2.1
  - 验收: 每个特征都有 SHAP 值分布图；Top 3 特征在金融逻辑上可解释

- [ ] **T2.5** — ML vs 线性因子样本外对比 — 预计: 6-8h
  - 产出: 对比报告（notebook）
  - 内容: ML 模型 vs 等权线性因子在 2022-2024 样本外全面对比
  - 依赖: T2.1, T1.8
  - 验收: ML 模型样本外 IC 均值比等权线性因子提高 ≥ 0.02，p < 0.05

- [ ] **T2.6** — (可选) PyTorch 时序模型原型 — 预计: 10-15h
  - 产出: `src/models/dl_models/`（LSTM/TFT 原型）
  - 内容: 与 LightGBM 对比；若显著优于 LightGBM 则纳入打分体系
  - 依赖: T2.1
  - 验收: 样本外表现不显著差于 LightGBM（至少持平）

### 交付物

- [ ] `src/models/ml_predictors.py` — ML 模型封装
- [ ] `src/models/dl_models/` — DL 模型目录（可选）
- [ ] 实验追踪表（DuckDB `experiment` 表）
- [ ] SHAP 可解释性报告

### 验收标准 (Gate Criteria)

- [ ] **ML 增量价值**: ML IC 均值比等权因子提高 ≥ 0.02（p < 0.05）
- [ ] **无过拟合**: Purged K-Fold 验证 IC 与样本外 IC 差异 < 30%
- [ ] **实验可复现**: 固定种子后两次训练产出相同权重（bitwise deterministic）
- [ ] **因子动量监控**: 滚动 3 个月 IC 为负的因子自动降权

---

## Phase 3: NLP 与智能扩展 (NLP & Intelligent Expansion) — 远期规划

### 目标
引入新闻/研报情绪因子，扩展标的池至高流动性个股。**不在当前开发周期内，仅作方向预留。**

### 前置依赖
- P2 全部验收通过

### 预留方向
- 非结构化文本数据处理（新闻、研报、社交媒体）
- FinBERT 情感分析
- 标的池扩展至个股（需重新评估生存者偏差、涨跌停、退市清算）
- Black-Litterman 组合优化
- 因子表达式引擎（遗传算法因子挖掘）

---

*基于 spec.md v2.0 §7 Phase Plan 创建 | 最后更新: 2025-07-18*
