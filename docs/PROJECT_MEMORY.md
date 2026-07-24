# PROJECT_MEMORY.md — Quant-Pilot 项目持久记忆

> **定位**: 项目的"外脑"。记录实际发生的事实、新增约束、设计变更。与 `task.md`（原始计划，只读）互补。
>
> **更新规则**: 演化记录只追加不删除。每次 Phase 完成或关键决策做出后必须更新。详细更新指令见 `prompts/update_memory_prompt.md`。

---

## 一、项目状态快照

| 字段                   | 值                                                                  |
| :--------------------- | :------------------------------------------------------------------ |
| **项目启动日期** | 2025-07-15                                                          |
| **当前 Phase**   | P0 — MVP 闭环                                                      |
| **当前 Task**    | T0.1-T0.6 ✅ → T0.7 就绪 |
| **Phase 进度**   | 6/10 交付物完成                                                     |
| **Gate 状态**    | 全部未通过                                                          |
| **最后更新**     | 2025-07-24 (Session 收尾交接)                                              |

---

## 二、Phase 执行快照

### Phase 0 — MVP 闭环 (进行中)

- **状态**: 🟡 T0.1-T0.6 ✅ → T0.7 待开始
- **开始日期**: 2025-07-18
- **预计完成**: 启动后 6-8 周
- **实际交付物**:
  - `pyproject.toml` — 项目配置 (P0 依赖) + CLI 入口修复
  - `config/settings.yaml` — 全局参数 + 19 ETF 标的池 (inline YAML) + Sina 数据源
  - `src/data_pipeline/fetchers/akshare_fetcher.py` — 双源获取 (Sina/eastmoney)
  - `src/data_pipeline/duckdb_manager.py` — DuckDB 6 表 + 幂等 UPSERT (33,313 rows)
  - `src/data_pipeline/processors/validator.py` — 5 项数据质量护栏
  - `src/strategies/macro_timing.py` — 宏观择时 (维度①②) + 仓位映射
  - `src/strategies/universe_filter.py` — 标的清洗 (4 项筛选 + 流动性警告)
  - `src/strategies/portfolio_optimizer.py` — 波动率倒数加权 + 迭代裁剪
  - `src/strategies/risk_manager.py` — ATR 移动止损 + 宏观骤降减仓 🆕
  - `data/quant_pilot.duckdb` — 19 ETFs 全量历史 OHLCV (33,313 rows)
  - `tests/test_duckdb_manager.py` — 14 tests ✅
  - `tests/test_validator.py` — 21 tests ✅
  - `tests/test_macro_timing.py` — 20 tests ✅
  - `tests/test_universe_filter.py` — 39 tests ✅
  - `tests/test_portfolio_optimizer.py` — 43 tests ✅
  - `tests/test_risk_manager.py` — 41 tests ✅ 🆕
- **Gate Criteria 状态**:
  - [ ] 闭环验收：daily_pipeline.py 完整跑通
  - [ ] 回测正确性：买入持有回测与指数偏差 < 1%
  - [ ] T+0/T+1 正确性：交收日期验证
  - [ ] 数据护栏有效：脏数据阻断测试
  - [ ] 信号可追溯：触发原因清晰可读
  - [ ] 无未来函数：至少一个自动化测试用例

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

### EV-006 — P0 NAV/IOPV 数据尚未入库

- **日期**: 2025-07-18
- **类型**: 发现依赖
- **与 task.md 关系**: 互补
- **描述**: T0.4 标的清洗模块的溢价检查需要 NAV/IOPV 数据, 但 T0.1 仅拉取了 OHLCV 日线数据 (akshare `fund_etf_hist_em` 可提供 `unit_nav` 字段但未使用)。当前溢价检查设计为: 若提供 NAV 数据则执行溢价过滤, 否则静默跳过。不影响其他 3 项筛选 (成交量/上市天数/集中度) 的正常运行。
- **影响范围**: T0.7 daily_pipeline 串联时, 若需要完整的溢价检查, 需在数据拉取步骤中增加 NAV 数据获取
- **后续动作**: P0 后期评估是否需要为溢价检查扩展数据拉取; 或延至 P1 因子计算时一并解决

---

## 四、关键产出索引

| 产出                                 | 路径                                              | Phase | 状态                         |
| :----------------------------------- | :------------------------------------------------ | :---- | :--------------------------- |
| 项目配置 + 依赖管理                  | `pyproject.toml`                                | P0    | ✅                           |
| 全局参数 + 20 ETF 标的池             | `config/settings.yaml`                          | P0    | ✅                           |
| 因子注册表 (空)                      | `config/factor_registry.yaml`                   | P0    | ✅ 待 P1-D1 填充             |
| 任务路线图                           | `docs/task.md`                                  | P0    | ✅ T0.1 已完成               |
| Akshare 双源获取 (Sina + eastmoney)  | `src/data_pipeline/fetchers/akshare_fetcher.py` | P0    | ✅ Sina 主力, eastmoney 备用 |
| DuckDB 数据库 (19 ETFs, 33,313 rows) | `data/quant_pilot.duckdb`                       | P0    | ✅ 全量历史数据已入库        |
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

---

## 五、未解决问题

| #    | 问题描述                                                                       | 发现日期   | 已尝试方案                                             | 需要帮助                                     |
| :--- | :----------------------------------------------------------------------------- | :--------- | :----------------------------------------------------- | :------------------------------------------- |
| I-01 | ~~eastmoney API 间歇性断连~~ → **已解决: 切换至 Sina 数据源** (EV-007) | 2025-07-18 | 反封锁休眠 / 代理绕过 / 指数退避重试 → 最终方案: Sina | 无需帮助                                     |
| I-02 | Python 3.14 与 vectorbt 不兼容 (P1 需要)                                       | 2025-07-18 | vectorbt 移入 optional-deps p1 分组                    | P1 时用`uv python pin 3.12` 或 Docker 隔离 |

---

## 六、下次会话起点

1. ~~T0.1: DuckDB 数据初始化~~ (已完成)
2. ~~T0.2: 数据质量护栏~~ (已完成)
3. ~~T0.3: 宏观择时模块~~ (已完成)
4. ~~T0.4: 标的清洗模块~~ (已完成)
5. ~~数据源修复: Sina 迁移 + 全量导入~~ (已完成 — EV-007, EV-008)
6. ~~T0.5: 组合优化模块~~ (已完成)
7. ~~T0.6: 风控模块~~ (已完成)
8. **当前**: 开始 **T0.7** — 每日盘后主流程编排 (串联全部漏斗)
9. ✅ 全量数据已入库: 19 ETFs, 33,313 rows (DuckDB)
10. ✅ 全量测试: 178/178 通过
11. 📋 五层漏斗已就绪: macro_timing → universe_filter → portfolio_optimizer → risk_manager
12. 📋 T0.7 依赖 T0.2-T0.6 全部模块; 需编排数据拉取→质检→择时→清洗→优化→风控→信号
13. 参考: `docs/task.md` → P0 T0.7; `docs/spec.md` §3 编排层
