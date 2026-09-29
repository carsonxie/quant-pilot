"""每日盘后主流程编排 — 串联五层决策漏斗.

流程 (每步记录日志与耗时):
  1. 增量拉取  — 库内有数据则从最近日期回溯 10 天补拉; 空库则全量
  2. 数据质检  — 阻断级问题 (负价格/单日涨跌幅>20%) 直接抛 DataQualityError 终止
  3. 幂等入库  — UPSERT OHLCV + ETF 元数据
  4. 宏观择时  — 当日得分; 截断一日重算前日得分 (供骤降检测, 无状态)
  5. 标的清洗  — 流动性/上市天数/溢价 (NAV 缺失记为 unknown, 不静默跳过, C10)
  6. 组合优化  — 按配置方法加权; scope=a_share 时宏观上限只约束 A 股合计,
                  非A (跨境/商品) 用独立上限, 不因 A 股信号被清零 (spec §5.2, C11)
  7. 风控执行  — ATR 止损剔除 + 宏观骤降压舱位, 触发时二次优化
  8. 信号生成  — 与昨日目标权重对比定 BUY/SELL/HOLD; 防抖仅约束常规调整,
                 明确退出(清零)/止损/骤降减仓不受防抖压制 (spec §5.4/§5.5, C04)
  9. 信号落库  — 确定性 signal_id, 重复运行幂等
 10. 调仓规划  — 账户存在时: 状态化止损 + 目标 vs 实际 → 建议落库 (M4);
                 账户不存在 → 保持研究输出, 不做任何写入

Usage:
    uv run daily   # 主流程完成后自动推送信号 (token 未配置时安全跳过)
    # 或代码调用
    result = run_daily_pipeline(settings, "data/quant_pilot.duckdb")
    print(result.summary())
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.fetchers.akshare_fetcher import AkshareFetcher
from data_pipeline.processors.validator import (
    DataQualityError,
    compute_historical_stats,
    validate_ohlcv,
)
from strategies import etf_pool, portfolio_optimizer, risk_manager
from strategies.macro_timing import MacroScore, MacroTiming

if TYPE_CHECKING:  # 避免与 rebalance_run 的运行时导入形成环
    from orchestration.rebalance_run import RebalanceRun

logger = logging.getLogger(__name__)

# ---- 常量 ----
BENCHMARK_SYMBOL = "510300"  # 沪深300ETF, 宏观择时趋势维度基准
REFETCH_BUFFER_DAYS = 10     # 增量拉取回溯缓冲 (自然日), 覆盖长假
SIGNAL_ID_DATE_FACTOR = 1000  # signal_id = 日期 * 1000 + 当日序号


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class Signal:
    """单条交易信号."""

    symbol: str
    direction: str        # BUY / SELL / HOLD
    target_weight: float  # 目标权重 0.0-1.0
    prev_weight: float    # 昨日目标权重
    trigger_reason: str   # 多层级归因


# 加权方法 → 中文标签 (信号归因可读性; C01: 算法标识与行为一致)
_METHOD_CN = {
    "inverse_volatility": "波动率倒数(1/σ)",
    "inverse_variance": "方差倒数(1/σ²)",
}


@dataclass
class PipelineResult:
    """每日主流程运行结果."""

    run_date: str                            # 信号日期 (最新交易日, ISO 格式)
    run_id: str = ""                         # 运行记录 ID (runs 表; A0-2)
    target_id: str = ""                      # 目标组合 ID (targets 表; M2b)
    pool_version: str = ""                   # 池版本 ID (universe_versions; M3)
    mode: str = "research"                   # 运行模式 (spec §10.1: research/paper/real)
    strategy_version: str = "unversioned"    # 策略版本 (settings.project.strategy_version)
    macro_scope: str = "a_share"             # 宏观信号作用域 (a_share | all_composite)
    macro_score: Optional[MacroScore] = None
    prev_macro_score: float = 50.0
    passed_symbols: list[str] = field(default_factory=list)
    universe_unknown: dict[str, list[str]] = field(default_factory=dict)
    portfolio: Optional[portfolio_optimizer.PortfolioResult] = None
    risk_report: Optional[risk_manager.RiskReport] = None
    signals: list[Signal] = field(default_factory=list)
    rebalance: RebalanceRun | None = None  # 调仓规划结果 (M4; 无账户=None)
    step_timings: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        """人类可读摘要 (供日志与未来 notifier 使用)."""
        lines = [f"===== Quant-Pilot 每日信号 ({self.run_date}) ====="]
        lines.append(
            f"[模式] {self.mode} | 策略版本 {self.strategy_version} | "
            f"数据截止 {self.run_date} | 宏观scope {self.macro_scope}"
        )

        if self.macro_score is not None:
            lines.append(f"[宏观择时] {self.macro_score.summary()}")

        clean_line = (
            f"[标的清洗] {len(self.passed_symbols)} 只通过: "
            f"{', '.join(self.passed_symbols) if self.passed_symbols else '(无)'}"
        )
        if self.universe_unknown:
            clean_line += (
                f" | {len(self.universe_unknown)} 只 unknown (NAV缺失, 待核验; 完整禁买规则见 M3)"
            )
        lines.append(clean_line)

        if self.portfolio is not None:
            lines.append(f"[组合优化]\n{self.portfolio.summary()}")

        if self.risk_report is not None:
            lines.append(f"[风控执行] {self.risk_report.message}")

        lines.append(f"[交易信号] 共 {len(self.signals)} 条:")
        for sig in self.signals:
            lines.append(
                f"  {sig.symbol} {sig.direction} "
                f"{sig.prev_weight:.1%} → {sig.target_weight:.1%} | {sig.trigger_reason}"
            )

        if self.rebalance is not None:
            lines.append(
                f"[调仓建议] 账户 {self.rebalance.account_id} | "
                f"新增 {self.rebalance.proposals_inserted} 条, "
                f"跳过 {self.rebalance.proposals_skipped} 项, "
                f"替代旧建议 {self.rebalance.replaced_open} 条"
            )
            if self.rebalance.stop_exits:
                lines.append(
                    f"  ⚠️ 状态化止损触发: {', '.join(self.rebalance.stop_exits)} "
                    f"(风险退出优先, 不受带宽压制)"
                )
            for prop in self.rebalance.plan.proposals:
                dep = f" [依赖 {prop.depends_on}]" if prop.depends_on else ""
                lines.append(
                    f"  {prop.side} {prop.symbol} {prop.shares:,} 份 "
                    f"≈ {prop.est_amount:,.0f} 元 (费 {prop.est_fee:,.0f}){dep} | {prop.reason}"
                )
            if self.rebalance.plan.paused:
                lines.append("  ⚠️ 账户关键约束未满足: 已暂停生成可执行数量")
        else:
            lines.append("[调仓建议] 未配置账户 (research 模式保持研究输出)")

        total = sum(self.step_timings.values())
        lines.append(f"[耗时] 全流程 {total:.1f}s " + self._timing_str())
        return "\n".join(lines)

    def _timing_str(self) -> str:
        parts = [f"{name}={secs:.1f}s" for name, secs in self.step_timings.items()]
        return "(" + ", ".join(parts) + ")" if parts else ""


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def run_daily_pipeline(
    settings: dict,
    db_path: str,
    fetcher: Optional[AkshareFetcher] = None,
    end_date: Optional[str] = None,
) -> PipelineResult:
    """执行每日盘后全流程.

    Args:
        settings: settings.yaml 解析后的完整字典
        db_path: DuckDB 文件路径
        fetcher: 数据获取器; None 时按 settings.data_fetch 创建 (便于测试注入)
        end_date: 数据截止日期 "YYYYMMDD"; None = 今天 (测试可固定日期)

    Returns:
        PipelineResult

    Raises:
        DataQualityError: 新拉取数据存在阻断级质量问题, 流程终止
    """
    timings: dict[str, float] = {}
    started_at = datetime.now()
    db = DuckDBManager(db_path)
    db.init_database()

    try:
        universe_etfs = _load_universe(settings)
        symbols = [etf["symbol"] for etf in universe_etfs]

        # ---- Step 1-3: 拉取 → 质检 → 入库 ----
        t0 = time.perf_counter()
        new_rows = _step_update_data(db, settings, symbols, universe_etfs,
                                     fetcher, end_date)
        timings["data_update"] = time.perf_counter() - t0
        logger.info("数据更新: 写入 %d 行, 耗时 %.1fs", new_rows, timings["data_update"])

        # ---- Step 4: 宏观择时 ----
        t0 = time.perf_counter()
        ohlcv = db.get_ohlcv(symbols=symbols)
        if ohlcv.is_empty():
            raise RuntimeError("ohlcv_daily 为空, 无法运行策略. 请先 uv run fetch-data")

        run_date = str(ohlcv["date"].max())
        run_id = f"daily-{run_date.replace('-', '')}-{started_at:%H%M%S}"
        # 目标 ID 按决策日确定 (同日重跑 → 同一目标 → 建议确定性去重, 幂等)
        target_id = f"target-{run_date.replace('-', '')}"
        macro = MacroTiming(
            ma_short=settings["macro_timing"].get("ma_short", 20),
            ma_long=settings["macro_timing"].get("ma_long", 60),
            weight_trend=settings["macro_timing"].get("weight_trend", 0.30),
            weight_breadth=settings["macro_timing"].get("weight_breadth", 0.25),
            plunge_threshold=settings["macro_timing"].get("score_plunge_threshold", 30.0),
        )
        score, prev_score = _step_macro_timing(macro, ohlcv)
        timings["macro_timing"] = time.perf_counter() - t0
        logger.info("宏观择时: %s (前日得分 %.1f)", score.summary(), prev_score)

        # ---- Step 5: 可交易池 (四层池层3; spec §4.2, M3) ----
        t0 = time.perf_counter()
        metadata = db.get_etf_metadata(symbols=symbols)
        date_ranges = db.get_symbol_date_ranges()
        pool_version = etf_pool.sync_instruments_and_pool(db, settings, symbols)
        nav_df = db.get_nav_daily(symbols)
        pool_report = etf_pool.build_pool_report(
            ohlcv, metadata_df=metadata, nav_df=nav_df,
            decision_date=date.fromisoformat(run_date),
            settings=settings,
            latest_version=db.get_latest_universe_version(),
            date_ranges=date_ranges,
        )
        timings["universe_filter"] = time.perf_counter() - t0
        logger.info("可交易池:\n%s", pool_report.summary())

        # ---- Step 6: 组合优化 (按宏观 scope 分桶: A股 / 非A) ----
        t0 = time.perf_counter()
        optimizer = portfolio_optimizer.from_settings(settings)
        scope = str(settings.get("macro_timing", {}).get("scope", "a_share"))
        non_a_cap = float(
            settings.get("portfolio", {}).get("non_a_share_max_total", 0.30)
        )
        market_types = {
            etf["symbol"]: etf.get("market_type", "A股") for etf in universe_etfs
        }
        portfolio = _step_optimize_scoped(
            optimizer, ohlcv, pool_report.buyable,
            score.position_cap, non_a_cap, market_types, scope,
        )
        timings["portfolio_optimize"] = time.perf_counter() - t0

        # ---- Step 7: 风控执行 ----
        t0 = time.perf_counter()
        rm = risk_manager.from_settings(settings)
        portfolio, risk_report = _step_risk_control(
            rm, optimizer, ohlcv, portfolio,
            current_score=score.composite_score,
            prev_score=prev_score,
            a_cap=score.position_cap,
            non_a_cap=non_a_cap,
            market_types=market_types,
            scope=scope,
        )
        timings["risk_control"] = time.perf_counter() - t0
        logger.info("风控执行: %s", risk_report.message)

        # ---- Step 8: 信号生成 ----
        t0 = time.perf_counter()
        prev_signals = db.get_latest_signals(before_date=run_date)
        turnover_threshold = settings.get("risk_management", {}).get(
            "turnover_threshold", 0.15,
        )
        signals = _step_generate_signals(
            portfolio, risk_report, score, prev_signals, turnover_threshold,
            stale_symbols=pool_report.stale_symbols,
        )
        timings["signal_generate"] = time.perf_counter() - t0

        # ---- Step 9: 信号落库 (含策略版本, A0-1) ----
        t0 = time.perf_counter()
        project_cfg = settings.get("project", {})
        _step_persist_signals(
            db, signals, run_date,
            strategy_version=str(project_cfg.get("strategy_version", "unversioned")),
        )
        timings["signal_persist"] = time.perf_counter() - t0

        # ---- Step 10: 调仓规划 (M4; 账户存在时生成建议) ----
        t0 = time.perf_counter()
        target_weights = {
            w.symbol: round(w.final_weight, 6) for w in portfolio.weights
        }
        target_weights["__CASH__"] = round(1.0 - sum(target_weights.values()), 6)
        rebalance = _step_rebalance(
            db, settings, ohlcv, target_weights,
            as_of=date.fromisoformat(run_date), target_id=target_id,
            pool_report=pool_report,
        )
        timings["rebalance"] = time.perf_counter() - t0

        result = PipelineResult(
            run_date=run_date,
            run_id=run_id,
            target_id=target_id,
            pool_version=pool_version,
            mode=str(project_cfg.get("mode", "research")),
            strategy_version=str(project_cfg.get("strategy_version", "unversioned")),
            macro_scope=scope,
            macro_score=score,
            prev_macro_score=prev_score,
            passed_symbols=pool_report.buyable,
            universe_unknown=pool_report.unknown,
            portfolio=portfolio,
            risk_report=risk_report,
            signals=signals,
            rebalance=rebalance,
            step_timings=timings,
        )
        _step_write_run(db, result, started_at, universe_size=len(symbols))
        _step_persist_target(db, result, universe_symbols=symbols)
        logger.info("\n%s", result.summary())
        return result

    finally:
        db.close()


# ------------------------------------------------------------------
# Step 10: 调仓规划 (M4; 账户存在时)
# ------------------------------------------------------------------

def _step_rebalance(
    db: DuckDBManager,
    settings: dict,
    ohlcv: pl.DataFrame,
    target_weights: dict[str, float],
    *,
    as_of: date,
    target_id: str,
    pool_report: etf_pool.PoolReport | None,
) -> RebalanceRun | None:
    """目标 vs 账户实际 → 调仓建议 (账户不存在时返回 None, 不写入).

    失败不阻断 daily: 数据/账户问题记录告警, 由 runs.detail_json 留痕.
    """
    try:
        from orchestration.rebalance_run import run_rebalance
    except ImportError:  # pragma: no cover - 理论不可达 (模块存在)
        logger.exception("调仓模块导入失败")
        return None

    try:
        rebalance = run_rebalance(
            db, settings, ohlcv, target_weights,
            as_of=as_of, target_id=target_id,
            pool_can_buy=set(pool_report.buyable) if pool_report else None,
            pool_can_sell=set(pool_report.sellable) if pool_report else None,
            stale_symbols=pool_report.stale_symbols if pool_report else None,
            ref_time=datetime.combine(as_of, datetime.min.time()) + timedelta(hours=15, minutes=30),
        )
    except Exception:
        logger.exception("调仓规划失败 (信号/目标已落库, 不影响流程成败)")
        return None
    if rebalance is not None:
        logger.info(
            "调仓建议: %d 条新增, %d 条跳过, %d 条旧建议已替代 (账户 %s)",
            rebalance.proposals_inserted, rebalance.proposals_skipped,
            rebalance.replaced_open, rebalance.account_id,
        )
    return rebalance


# ------------------------------------------------------------------
# Step 1-3: 数据拉取 → 质检 → 入库
# ------------------------------------------------------------------

def _step_update_data(
    db: DuckDBManager,
    settings: dict,
    symbols: list[str],
    universe_etfs: list[dict],
    fetcher: Optional[AkshareFetcher],
    end_date: Optional[str],
) -> int:
    """增量拉取 + 质检 + 幂等入库. 返回写入行数."""
    if fetcher is None:
        fetch_cfg = settings.get("data_fetch", {})
        fetcher = AkshareFetcher(
            source=fetch_cfg.get("source", "sina"),
            min_sleep=fetch_cfg.get("min_sleep", 0.1),
            max_sleep=fetch_cfg.get("max_sleep", 0.5),
            max_retries=fetch_cfg.get("max_retries", 3),
            retry_backoff_base=fetch_cfg.get("retry_backoff_base", 5.0),
        )

    # 决定拉取区间: 库内有数据 → 最近日期回溯缓冲; 空库 → 全量
    _, max_date = db.get_date_range()
    if max_date is not None:
        latest = date.fromisoformat(str(max_date))
        start = (latest - timedelta(days=REFETCH_BUFFER_DAYS)).strftime("%Y%m%d")
        logger.info("增量更新: 从 %s 补拉 (库内最新 %s)", start, max_date)
    else:
        start = settings.get("data_fetch", {}).get("default_start_date", "20190101")
        logger.info("空库, 全量拉取: 从 %s 开始", start)

    end = end_date or time.strftime("%Y%m%d")

    new_df = fetcher.fetch(symbols, start, end)
    if new_df.is_empty():
        logger.warning("本次未拉到新数据 (可能非交易日), 沿用库内数据继续")
        return 0

    # 质检: 阻断级问题抛 DataQualityError 终止流程 (产品感知涨跌幅, C10)
    # 已登记权益事件 (M8/I-09 方案1) → 命中登记日期与比例的超限跳变不阻断
    historical = db.get_ohlcv(symbols=symbols)
    stats = compute_historical_stats(historical)
    dq_cfg = settings.get("data_quality", {}).get("daily_change", {})
    limits = {
        sym: float(pct) / 100.0
        for sym, pct in dq_cfg.get("limit_overrides_pct", {}).items()
    }
    issues = validate_ohlcv(
        new_df,
        historical_stats=stats,
        price_limits=limits,
        default_price_limit=float(dq_cfg.get("default_limit_pct", 10.0)) / 100.0,
        price_limit_buffer=float(dq_cfg.get("review_buffer_pct", 2.0)) / 100.0,
        registered_equity_events=db.get_equity_events(),
    )
    criticals = [i for i in issues if i.severity == "critical"]
    if criticals:
        raise DataQualityError(criticals)

    # 幂等入库
    meta_rows = [
        {
            "symbol": etf["symbol"],
            "name": etf["name"],
            "market_type": etf["market_type"],
            "settlement_type": etf["settlement"],
            "list_date": None,
            "avg_daily_volume_20d": None,
        }
        for etf in universe_etfs
    ]
    db.upsert_etf_metadata(pl.DataFrame(meta_rows))
    return db.upsert_ohlcv(new_df)


# ------------------------------------------------------------------
# Step 4: 宏观择时 (当日得分 + 无状态前日得分)
# ------------------------------------------------------------------

def _step_macro_timing(
    macro: MacroTiming,
    ohlcv: pl.DataFrame,
) -> tuple[MacroScore, float]:
    """计算当日宏观得分, 并截断最新一日重算前日得分.

    前日得分用于骤降检测. 通过数据截断重算而非持久化状态,
    保证幂等且无状态漂移.
    """
    benchmark = ohlcv.filter(pl.col("symbol") == BENCHMARK_SYMBOL)
    if benchmark.is_empty():
        logger.warning("基准 %s 无数据, 用全标的池近似趋势维度", BENCHMARK_SYMBOL)
        benchmark = ohlcv

    score = macro.evaluate(benchmark, ohlcv)

    latest_date = ohlcv["date"].max()
    prev_ohlcv = ohlcv.filter(pl.col("date") < latest_date)
    if prev_ohlcv.is_empty():
        logger.warning("无历史数据, 前日得分取中性值 50")
        return score, 50.0

    prev_benchmark = prev_ohlcv.filter(pl.col("symbol") == BENCHMARK_SYMBOL)
    if prev_benchmark.is_empty():
        prev_benchmark = prev_ohlcv
    prev_score = macro.evaluate(prev_benchmark, prev_ohlcv).composite_score

    return score, prev_score


# ------------------------------------------------------------------
# Step 6: 组合优化
# ------------------------------------------------------------------

def _step_optimize(
    optimizer: portfolio_optimizer.PortfolioOptimizer,
    ohlcv: pl.DataFrame,
    symbols: list[str],
    position_cap: float,
) -> portfolio_optimizer.PortfolioResult:
    """按配置的加权方法 (inverse_volatility / inverse_variance) 优化目标组合."""
    if not symbols or position_cap <= 0:
        logger.info("无可优化标的或仓位上限为 0 (cap=%.2f), 目标空仓", position_cap)
        return portfolio_optimizer.PortfolioResult(
            weights=[], position_cap=position_cap, total_weight=0.0,
            method=optimizer.method,
            num_symbols_input=len(symbols), num_symbols_output=0,
        )

    candidates = ohlcv.filter(pl.col("symbol").is_in(symbols))
    portfolio = optimizer.optimize(candidates, position_cap=position_cap)
    logger.info("组合优化:\n%s", portfolio.summary())
    return portfolio


def _empty_portfolio(method: str) -> portfolio_optimizer.PortfolioResult:
    """空分桶结果."""
    return portfolio_optimizer.PortfolioResult(
        weights=[], position_cap=0.0, total_weight=0.0, method=method,
        num_symbols_input=0, num_symbols_output=0,
    )


def _merge_portfolio_results(
    a: portfolio_optimizer.PortfolioResult,
    b: portfolio_optimizer.PortfolioResult,
    method: str,
    total_cap: float,
    n_input: int,
) -> portfolio_optimizer.PortfolioResult:
    """合并两个分桶结果 (权重降序; position_cap = 两桶上限之和)."""
    weights = sorted(
        a.weights + b.weights, key=lambda w: w.final_weight, reverse=True,
    )
    vols = [w.annualized_vol for w in weights]
    return portfolio_optimizer.PortfolioResult(
        weights=weights,
        position_cap=round(total_cap, 6),
        total_weight=round(sum(w.final_weight for w in weights), 6),
        method=method,
        vol_lookback=a.vol_lookback,
        max_position_weight=a.max_position_weight,
        num_symbols_input=n_input,
        num_symbols_output=len(weights),
        min_annual_vol=min(vols) if vols else None,
        max_annual_vol=max(vols) if vols else None,
    )


def _step_optimize_scoped(
    optimizer: portfolio_optimizer.PortfolioOptimizer,
    ohlcv: pl.DataFrame,
    symbols: list[str],
    a_share_cap: float,
    non_a_cap: float,
    market_types: dict[str, str],
    scope: str = "a_share",
) -> portfolio_optimizer.PortfolioResult:
    """按宏观信号 scope 生成目标组合 (spec §5.2, C11).

    - scope="a_share" (默认): 宏观舱位上限只约束 A 股合计; 非 A (跨境/商品)
      用独立上限 min(non_a_share_max_total, 1 − A股合计), 不因 A 股信号清零.
    - scope="all_composite": 显式研究版本 — 宏观上限约束全部资产 (v0.1 旧行为).
    """
    if scope != "a_share":
        return _step_optimize(optimizer, ohlcv, symbols, a_share_cap)

    a_symbols = [s for s in symbols if market_types.get(s, "A股") == "A股"]
    non_symbols = [s for s in symbols if market_types.get(s, "A股") != "A股"]

    res_a = _step_optimize(optimizer, ohlcv, a_symbols, a_share_cap)
    if non_symbols:
        remaining = max(0.0, 1.0 - res_a.total_weight)
        eff_non_cap = min(non_a_cap, remaining)
        res_non = _step_optimize(optimizer, ohlcv, non_symbols, eff_non_cap)
    else:
        eff_non_cap = 0.0
        res_non = _empty_portfolio(optimizer.method)

    return _merge_portfolio_results(
        res_a, res_non, optimizer.method,
        total_cap=a_share_cap + eff_non_cap, n_input=len(symbols),
    )


# ------------------------------------------------------------------
# Step 7: 风控执行 (必要时二次优化)
# ------------------------------------------------------------------

def _step_risk_control(
    rm: risk_manager.RiskManager,
    optimizer: portfolio_optimizer.PortfolioOptimizer,
    ohlcv: pl.DataFrame,
    portfolio: portfolio_optimizer.PortfolioResult,
    current_score: float,
    prev_score: float,
    a_cap: float | None = None,
    non_a_cap: float = 0.0,
    market_types: dict[str, str] | None = None,
    scope: str = "a_share",
) -> tuple[portfolio_optimizer.PortfolioResult, risk_manager.RiskReport]:
    """ATR 止损 + 宏观骤降减仓.

    若触发止损或骤降, 剔除止损标的并用有效上限二次优化.
    scope=a_share 时, 风控报告与骤降压舱均按 A 股口径 (非A由独立上限约束).
    """
    # 风险报告使用的宏观舱位口径 (a_share scope 下即 A 股合计上限)
    report_cap = (
        a_cap if (scope == "a_share" and a_cap is not None)
        else portfolio.position_cap
    )
    held_symbols = [w.symbol for w in portfolio.weights]
    if not held_symbols:
        # 空仓场景仍需检测宏观骤降
        report = rm.evaluate(
            ohlcv.head(0), current_score, prev_score,
            position_cap=report_cap, symbols=[],
        )
        return portfolio, report

    held_ohlcv = ohlcv.filter(pl.col("symbol").is_in(held_symbols))
    report = rm.evaluate(
        held_ohlcv, current_score, prev_score,
        position_cap=report_cap,
    )

    need_rebalance = bool(report.forced_exits) or (
        report.effective_position_cap < report_cap
    )
    if need_rebalance:
        original_exits = list(report.forced_exits)
        survivors = [s for s in held_symbols if s not in original_exits]
        logger.warning(
            "风控触发再平衡: 剔除 %s, 舱位上限 %.2f → %.2f (scope=%s)",
            report.forced_exits, report_cap,
            report.effective_position_cap, scope,
        )
        if scope == "a_share" and market_types is not None:
            portfolio = _step_optimize_scoped(
                optimizer, ohlcv, survivors,
                report.effective_position_cap, non_a_cap, market_types, scope,
            )
            scope_note = (
                f" (宏观scope=A股: 上限 {report.effective_position_cap:.0%}; "
                f"非A独立上限 {non_a_cap:.0%})"
            )
        else:
            portfolio = _step_optimize(
                optimizer, ohlcv, survivors, report.effective_position_cap,
            )
            scope_note = ""
        # 二次优化后刷新止损状态 (仓位变化不影响止损线, 但标的集可能变化)
        final_symbols = [w.symbol for w in portfolio.weights]
        report = rm.evaluate(
            ohlcv.filter(pl.col("symbol").is_in(final_symbols)) if final_symbols
            else ohlcv.head(0),
            current_score, prev_score,
            position_cap=report.effective_position_cap,
            symbols=final_symbols,
        )
        # 保留原始止损退出名单, 供信号归因使用
        report.forced_exits = original_exits
        if scope_note:
            report.message += scope_note
    return portfolio, report


# ------------------------------------------------------------------
# Step 8: 信号生成 (与昨日目标权重对比)
# ------------------------------------------------------------------

def _step_generate_signals(
    portfolio: portfolio_optimizer.PortfolioResult,
    risk_report: risk_manager.RiskReport,
    score: MacroScore,
    prev_signals: pl.DataFrame,
    turnover_threshold: float,
    stale_symbols: set[str] | None = None,
) -> list[Signal]:
    """对比新旧目标权重生成 BUY/SELL/HOLD 信号.

    规则 (spec §5.4/§5.5, C04/C12: 普通调整规则不能压制风险或明确退出):
      - 明确退出(目标清零)、止损剔除、宏观骤降减仓 → 无条件 SELL, 不受防抖压制
      - 首次建仓 (prev=0 且 new>0) → BUY, 不因普通带宽永久跳过 (spec §5.4)
      - 停牌/无有效价格 (stale): 无法卖出 → 保留原目标 (HOLD), 风险持续展示
      - 其余 |Δweight| < turnover_threshold → HOLD (维持昨日目标)
      - 常规增加 → BUY; 常规减少 → SELL
    """
    new_weights = {w.symbol: w.final_weight for w in portfolio.weights}

    prev_weights: dict[str, float] = {}
    if not prev_signals.is_empty():
        for row in prev_signals.iter_rows(named=True):
            prev_weights[row["symbol"]] = float(row["target_weight"])

    forced_exits = set(risk_report.forced_exits)
    all_symbols = sorted(set(new_weights) | set(prev_weights))
    signals: list[Signal] = []

    for sym in all_symbols:
        new_w = new_weights.get(sym, 0.0)
        prev_w = prev_weights.get(sym, 0.0)
        raw_delta = new_w - prev_w

        # 风险/明确退出/首次建仓不受防抖约束 (留痕原因, 供归因)
        bypass_reason: str | None = None
        if raw_delta < 0.0:
            if sym in forced_exits:
                bypass_reason = "风险退出(止损)"
            elif new_w <= 0.0:
                bypass_reason = "明确退出(目标清零)"
            elif risk_report.plunge_detected:
                bypass_reason = "风险减仓(宏观骤降)"
        elif raw_delta > 0.0 and prev_w <= 0.0:
            bypass_reason = "首次建仓"

        # 停牌/无有效价格: 不能卖出, 保留原目标待可交易 (spec §4.4/§5.5)
        hold_reason: str | None = None
        stale_hold = (
            raw_delta < 0.0
            and bool(stale_symbols)
            and sym in stale_symbols
            and prev_w > 0.0
        )
        if stale_hold:
            direction = "HOLD"
            new_w = prev_w
            hold_reason = "停牌/无有效价格: 无法卖出, 保留原目标待可交易"
        elif raw_delta < 0.0 and bypass_reason is not None:
            direction = "SELL"
        elif raw_delta > 0.0 and bypass_reason is not None:
            direction = "BUY"
        elif abs(raw_delta) < turnover_threshold:
            direction = "HOLD"
            new_w = prev_w  # 维持昨日目标, 避免无效调仓
        elif raw_delta > 0.0:
            direction = "BUY"
        else:
            direction = "SELL"

        reason = _build_trigger_reason(
            sym, direction, new_w, prev_w, raw_delta,
            turnover_threshold, portfolio, risk_report, score,
            bypass_reason, hold_reason,
        )
        signals.append(Signal(
            symbol=sym, direction=direction,
            target_weight=round(new_w, 6), prev_weight=round(prev_w, 6),
            trigger_reason=reason,
        ))

    return signals


def _build_trigger_reason(
    symbol: str,
    direction: str,
    new_w: float,
    prev_w: float,
    raw_delta: float,
    turnover_threshold: float,
    portfolio: portfolio_optimizer.PortfolioResult,
    risk_report: risk_manager.RiskReport,
    score: MacroScore,
    bypass_reason: str | None,
    hold_reason: str | None = None,
) -> str:
    """构建多层级归因字符串 (信号可追溯)."""
    parts = [
        f"宏观{score.composite_score:.0f}分({score.regime},上限{score.position_cap:.0%})",
    ]

    weight = next((w for w in portfolio.weights if w.symbol == symbol), None)
    if weight is not None:
        method_cn = _METHOD_CN.get(portfolio.method, portfolio.method)
        parts.append(
            f"{method_cn}加权{weight.final_weight:.1%}"
            f"(年化波动{weight.annualized_vol:.0%})"
        )
    else:
        parts.append("未入选组合")

    if risk_report.plunge_detected:
        parts.append(
            f"宏观骤降{risk_report.plunge_score_drop:.0f}分,"
            f"仓位压至{risk_report.effective_position_cap:.0%}"
        )
    if symbol in set(risk_report.forced_exits):
        parts.append("ATR止损触发,强制退出")
    if bypass_reason is not None:
        parts.append(f"{bypass_reason}不受防抖限制")

    if direction == "HOLD":
        if hold_reason:
            parts.append(hold_reason)
        else:
            parts.append(
                f"变动{abs(raw_delta):.1%}<防抖阈值{turnover_threshold:.0%},维持原仓"
            )
    else:
        parts.append(f"{prev_w:.1%}→{new_w:.1%}")

    return "; ".join(parts)


# ------------------------------------------------------------------
# Step 9: 信号落库 (幂等)
# ------------------------------------------------------------------

def _step_persist_signals(
    db: DuckDBManager,
    signals: list[Signal],
    run_date: str,
    strategy_version: str = "unversioned",
) -> None:
    """写入信号, signal_id 确定性生成保证幂等; 携带策略版本 (A0-1)."""
    if not signals:
        logger.info("无信号需要写入")
        return

    date_int = int(run_date.replace("-", ""))
    signal_date = date.fromisoformat(run_date)
    rows = [
        {
            "signal_id": date_int * SIGNAL_ID_DATE_FACTOR + idx,
            "signal_date": signal_date,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "target_weight": sig.target_weight,
            "trigger_reason": sig.trigger_reason,
            "strategy_version": strategy_version,
        }
        for idx, sig in enumerate(signals)
    ]
    db.upsert_signals(pl.DataFrame(rows))


# ------------------------------------------------------------------
# 运行记录 (runs 表; A0-2 / M2)
# ------------------------------------------------------------------

def _step_write_run(
    db: DuckDBManager,
    result: PipelineResult,
    started_at: datetime,
    universe_size: int,
) -> None:
    """写入运行记录 (结果/状态/数据版本可追踪)."""
    detail = {
        "signals": len(result.signals),
        "passed": len(result.passed_symbols),
        "unknown": len(result.universe_unknown),
        "universe_size": universe_size,
        "macro_score": (
            result.macro_score.composite_score if result.macro_score else None
        ),
        "position_cap": (
            result.macro_score.position_cap if result.macro_score else None
        ),
        "portfolio": [
            {"symbol": w.symbol, "weight": round(w.final_weight, 6)}
            for w in (result.portfolio.weights if result.portfolio else [])
        ],
        "rebalance": (
            {
                "account_id": result.rebalance.account_id,
                "proposals": result.rebalance.proposals_inserted,
                "skipped": result.rebalance.proposals_skipped,
                "replaced": result.rebalance.replaced_open,
                "stop_exits": result.rebalance.stop_exits,
                "paused": result.rebalance.plan.paused,
            }
            if result.rebalance
            else None
        ),
    }
    db.insert_run(pl.DataFrame([{
        "run_id": result.run_id,
        "run_type": "daily",
        "run_date": date.fromisoformat(result.run_date),
        "mode": result.mode,
        "strategy_version": result.strategy_version,
        "data_version": result.run_date,
        "status": "ok",
        "detail_json": json.dumps(detail, ensure_ascii=False),
        "started_at": started_at,
        "finished_at": datetime.now(),
    }]))


def _record_failed_run(db_path: str, status: str, error: str) -> None:
    """失败/阻断运行也留痕 (spec §6.5: 运行状态可区分; 不静默失败)."""
    now = datetime.now()
    run_id = f"daily-{now:%Y%m%d}-{now:%H%M%S}-{status}"
    try:
        db = DuckDBManager(db_path)
        try:
            db.insert_run(pl.DataFrame([{
                "run_id": run_id,
                "run_type": "daily",
                "run_date": date.today(),
                "status": status,
                "detail_json": json.dumps({"error": error}, ensure_ascii=False),
                "started_at": now,
                "finished_at": now,
            }]))
        finally:
            db.close()
    except Exception:
        logger.exception("失败运行记录写入失败")


# ------------------------------------------------------------------
# 目标组合 (四类记录之 Target; M2b)
# ------------------------------------------------------------------

def _step_persist_target(
    db: DuckDBManager,
    result: PipelineResult,
    universe_symbols: list[str],
) -> None:
    """写入目标组合: 完整权重 (含现金 __CASH__) + 版本字段 + 原因.

    新目标生效后, 引用旧目标的开放建议标记已替代 (spec §3.2).
    """
    weights = {
        w.symbol: round(w.final_weight, 6)
        for w in (result.portfolio.weights if result.portfolio else [])
    }
    cash_weight = round(1.0 - sum(weights.values()), 6)
    pool_version = result.pool_version
    if not pool_version:
        pool_digest = hashlib.sha1(
            "|".join(sorted(universe_symbols)).encode("utf-8"),
        ).hexdigest()[:10]
        pool_version = f"uv-{pool_digest}"

    macro = result.macro_score
    macro_part = (
        f"macro={macro.composite_score:.0f}({macro.regime},cap={macro.position_cap:.0%})"
        if macro else "macro=-"
    )
    reason = (
        f"{macro_part}; scope={result.macro_scope}; "
        f"risk={result.risk_report.message if result.risk_report else '-'}"
    )

    db.upsert_target(pl.DataFrame([{
        "target_id": result.target_id,
        "strategy_id": "five-funnel",
        "strategy_version": result.strategy_version,
        "mode": result.mode,
        "as_of": date.fromisoformat(result.run_date),
        "decision_time": datetime.now(),
        "data_version": result.run_date,
        "pool_version": pool_version,
        "config_version": result.strategy_version,
        "reason": reason,
    }]))

    weight_rows = [
        {"target_id": result.target_id, "symbol": symbol, "target_weight": weight}
        for symbol, weight in weights.items()
    ]
    weight_rows.append({
        "target_id": result.target_id,
        "symbol": "__CASH__",
        "target_weight": cash_weight,
    })
    db.replace_target_weights(result.target_id, pl.DataFrame(weight_rows))

    replaced = db.mark_targets_replaced(result.target_id)
    if replaced:
        logger.info("新目标生效: %d 条旧建议标记 replaced", replaced)


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def _load_universe(settings: dict) -> list[dict]:
    """从 settings 解析标的池 (与 fetcher main 同一约定)."""
    universe = settings.get("universe", {})
    all_etfs: list[dict] = []
    for category in universe.values():
        if isinstance(category, list):
            all_etfs.extend(category)
    return all_etfs


# ------------------------------------------------------------------
# T0.8: 信号推送 (CLI 成功后调用)
# ------------------------------------------------------------------

def _push_signals(settings: dict, result: PipelineResult, db_path: str) -> None:
    """推送信号到移动端; 失败只告警, 不影响退出码 (信号已落库).

    推送结果持久化到运行记录 (runs.push_ok/attempts/error) 并结构化日志留痕.
    """
    from orchestration.notifier import push_pipeline_result

    try:
        push_result = push_pipeline_result(settings, result)
    except Exception:
        logger.exception("信号推送异常 (信号已落库, 不影响流程成败)")
        return
    level = logging.INFO if push_result.ok else logging.WARNING
    logger.log(
        level,
        "push_result ok=%s channel=%s attempts=%d error=%s",
        push_result.ok, push_result.channel,
        push_result.attempts, push_result.error,
    )
    if result.run_id:
        try:
            run_db = DuckDBManager(db_path)
            try:
                run_db.update_run_push(
                    result.run_id, push_result.ok,
                    push_result.attempts, push_result.error,
                )
            finally:
                run_db.close()
        except Exception:
            logger.exception("运行记录推送状态更新失败 (不影响退出码)")


# ------------------------------------------------------------------
# CLI 入口 (uv run daily)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 每日盘后主流程."""
    import sys
    from pathlib import Path

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_root = Path(__file__).resolve().parents[2]
    settings_path = project_root / "config" / "settings.yaml"
    if not settings_path.exists():
        logger.error("settings.yaml not found at %s", settings_path)
        sys.exit(1)

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    db_path = project_root / settings["storage"]["db_path"]

    try:
        result = run_daily_pipeline(settings, str(db_path))
        # Windows GBK 控制台无法编码 emoji, 重配 stdout 防 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(result.summary())
        _push_signals(settings, result, str(db_path))
    except DataQualityError as exc:
        logger.exception("数据质检阻断, 流程终止")
        _record_failed_run(str(db_path), "blocked", str(exc)[:500])
        sys.exit(2)
    except Exception as exc:
        logger.exception("每日流程执行失败")
        _record_failed_run(str(db_path), "failed", repr(exc)[:500])
        sys.exit(1)


if __name__ == "__main__":
    main()
