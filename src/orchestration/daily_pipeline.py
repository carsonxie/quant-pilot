"""每日盘后主流程编排 — 串联五层决策漏斗.

流程 (每步记录日志与耗时):
  1. 增量拉取  — 库内有数据则从最近日期回溯 10 天补拉; 空库则全量
  2. 数据质检  — 阻断级问题 (负价格/单日涨跌幅>20%) 直接抛 DataQualityError 终止
  3. 幂等入库  — UPSERT OHLCV + ETF 元数据
  4. 宏观择时  — 当日得分; 截断一日重算前日得分 (供骤降检测, 无状态)
  5. 标的清洗  — 流动性/上市天数/溢价 (NAV 缺失时静默跳过)
  6. 组合优化  — 波动率倒数加权, 归一化到仓位上限
  7. 风控执行  — ATR 止损剔除 + 宏观骤降压仓位, 触发时二次优化
  8. 信号生成  — 与昨日目标权重对比定 BUY/SELL/HOLD (turnover_threshold 防抖)
  9. 信号落库  — 确定性 signal_id, 重复运行幂等

Usage:
    uv run daily   # 主流程完成后自动推送信号 (token 未配置时安全跳过)
    # 或代码调用
    result = run_daily_pipeline(settings, "data/quant_pilot.duckdb")
    print(result.summary())
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.fetchers.akshare_fetcher import AkshareFetcher
from data_pipeline.processors.validator import (
    DataQualityError,
    compute_historical_stats,
    validate_ohlcv,
)
from strategies import portfolio_optimizer, risk_manager, universe_filter
from strategies.macro_timing import MacroScore, MacroTiming

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


@dataclass
class PipelineResult:
    """每日主流程运行结果."""

    run_date: str                            # 信号日期 (最新交易日, ISO 格式)
    macro_score: Optional[MacroScore] = None
    prev_macro_score: float = 50.0
    passed_symbols: list[str] = field(default_factory=list)
    portfolio: Optional[portfolio_optimizer.PortfolioResult] = None
    risk_report: Optional[risk_manager.RiskReport] = None
    signals: list[Signal] = field(default_factory=list)
    step_timings: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        """人类可读摘要 (供日志与未来 notifier 使用)."""
        lines = [f"===== Quant-Pilot 每日信号 ({self.run_date}) ====="]

        if self.macro_score is not None:
            lines.append(f"[宏观择时] {self.macro_score.summary()}")

        lines.append(
            f"[标的清洗] {len(self.passed_symbols)} 只通过: "
            f"{', '.join(self.passed_symbols) if self.passed_symbols else '(无)'}"
        )

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

        # ---- Step 5: 标的清洗 ----
        t0 = time.perf_counter()
        uf = universe_filter.from_settings(settings)
        metadata = db.get_etf_metadata(symbols=symbols)
        filter_result = uf.filter(ohlcv, metadata_df=metadata, nav_df=None)
        timings["universe_filter"] = time.perf_counter() - t0
        logger.info("标的清洗:\n%s", filter_result.summary())

        # ---- Step 6: 组合优化 ----
        t0 = time.perf_counter()
        optimizer = portfolio_optimizer.from_settings(settings)
        portfolio = _step_optimize(optimizer, ohlcv, filter_result.passed,
                                   score.position_cap)
        timings["portfolio_optimize"] = time.perf_counter() - t0

        # ---- Step 7: 风控执行 ----
        t0 = time.perf_counter()
        rm = risk_manager.from_settings(settings)
        portfolio, risk_report = _step_risk_control(
            rm, optimizer, ohlcv, portfolio,
            current_score=score.composite_score,
            prev_score=prev_score,
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
        )
        timings["signal_generate"] = time.perf_counter() - t0

        # ---- Step 9: 信号落库 ----
        t0 = time.perf_counter()
        _step_persist_signals(db, signals, run_date)
        timings["signal_persist"] = time.perf_counter() - t0

        result = PipelineResult(
            run_date=run_date,
            macro_score=score,
            prev_macro_score=prev_score,
            passed_symbols=filter_result.passed,
            portfolio=portfolio,
            risk_report=risk_report,
            signals=signals,
            step_timings=timings,
        )
        logger.info("\n%s", result.summary())
        return result

    finally:
        db.close()


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

    # 质检: 阻断级问题抛 DataQualityError 终止流程
    historical = db.get_ohlcv(symbols=symbols)
    stats = compute_historical_stats(historical)
    issues = validate_ohlcv(new_df, historical_stats=stats)
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
    """对通过清洗的标的执行波动率倒数加权."""
    if not symbols or position_cap <= 0:
        logger.info("无可优化标的或仓位上限为 0 (cap=%.2f), 目标空仓", position_cap)
        return portfolio_optimizer.PortfolioResult(
            weights=[], position_cap=position_cap, total_weight=0.0,
            num_symbols_input=len(symbols), num_symbols_output=0,
        )

    candidates = ohlcv.filter(pl.col("symbol").is_in(symbols))
    portfolio = optimizer.optimize(candidates, position_cap=position_cap)
    logger.info("组合优化:\n%s", portfolio.summary())
    return portfolio


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
) -> tuple[portfolio_optimizer.PortfolioResult, risk_manager.RiskReport]:
    """ATR 止损 + 宏观骤降减仓.

    若触发止损或骤降, 剔除止损标的并用有效仓位上限二次优化.
    """
    held_symbols = [w.symbol for w in portfolio.weights]
    if not held_symbols:
        # 空仓场景仍需检测宏观骤降
        report = rm.evaluate(
            ohlcv.head(0), current_score, prev_score,
            position_cap=portfolio.position_cap, symbols=[],
        )
        return portfolio, report

    held_ohlcv = ohlcv.filter(pl.col("symbol").is_in(held_symbols))
    report = rm.evaluate(
        held_ohlcv, current_score, prev_score,
        position_cap=portfolio.position_cap,
    )

    need_rebalance = bool(report.forced_exits) or (
        report.effective_position_cap < portfolio.position_cap
    )
    if need_rebalance:
        original_exits = list(report.forced_exits)
        survivors = [s for s in held_symbols if s not in original_exits]
        logger.warning(
            "风控触发再平衡: 剔除 %s, 仓位上限 %.2f → %.2f",
            report.forced_exits, portfolio.position_cap,
            report.effective_position_cap,
        )
        portfolio = _step_optimize(
            optimizer, ohlcv, survivors, report.effective_position_cap,
        )
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
) -> list[Signal]:
    """对比新旧目标权重生成 BUY/SELL/HOLD 信号.

    - |Δweight| < turnover_threshold → HOLD (防抖, 不触发调仓)
    - 昨日有仓今日无 → SELL (清仓)
    - 其余按权重增减定方向
    """
    new_weights = {w.symbol: w.final_weight for w in portfolio.weights}

    prev_weights: dict[str, float] = {}
    if not prev_signals.is_empty():
        for row in prev_signals.iter_rows(named=True):
            prev_weights[row["symbol"]] = float(row["target_weight"])

    all_symbols = sorted(set(new_weights) | set(prev_weights))
    signals: list[Signal] = []

    for sym in all_symbols:
        new_w = new_weights.get(sym, 0.0)
        prev_w = prev_weights.get(sym, 0.0)
        delta = new_w - prev_w

        if abs(delta) < turnover_threshold:
            direction = "HOLD"
            new_w = prev_w  # 维持昨日目标, 避免无效调仓
        elif delta > 0:
            direction = "BUY"
        else:
            direction = "SELL"

        reason = _build_trigger_reason(
            sym, direction, new_w, prev_w, portfolio, risk_report, score,
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
    portfolio: portfolio_optimizer.PortfolioResult,
    risk_report: risk_manager.RiskReport,
    score: MacroScore,
) -> str:
    """构建多层级归因字符串 (信号可追溯)."""
    parts = [
        f"宏观{score.composite_score:.0f}分({score.regime},上限{score.position_cap:.0%})",
    ]

    if symbol in {w.symbol for w in portfolio.weights}:
        weight = next(w for w in portfolio.weights if w.symbol == symbol)
        parts.append(f"逆波动率加权{weight.final_weight:.1%}(年化波动{weight.annualized_vol:.0%})")
    else:
        parts.append("未入选组合")

    if risk_report.plunge_detected:
        parts.append(
            f"宏观骤降{risk_report.plunge_score_drop:.0f}分,"
            f"仓位压至{risk_report.effective_position_cap:.0%}"
        )
    if symbol in risk_report.forced_exits:
        parts.append("ATR止损触发,强制退出")

    if direction == "HOLD":
        parts.append(f"变动<{abs(new_w - prev_w):.1%}防抖阈值,维持原仓")
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
) -> None:
    """写入信号, signal_id 确定性生成保证幂等."""
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
        }
        for idx, sig in enumerate(signals)
    ]
    db.upsert_signals(pl.DataFrame(rows))


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

def _push_signals(settings: dict, result: PipelineResult) -> None:
    """推送信号到移动端; 失败只告警, 不影响退出码 (信号已落库)."""
    from orchestration.notifier import push_pipeline_result

    try:
        push_result = push_pipeline_result(settings, result)
    except Exception:
        logger.exception("信号推送异常 (信号已落库, 不影响流程成败)")
        return
    if push_result.ok:
        logger.info(
            "信号已推送到手机 (channel=%s, 尝试 %d 次)",
            push_result.channel, push_result.attempts,
        )
    else:
        logger.warning("信号推送未完成: %s", push_result.error)


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
        _push_signals(settings, result)
    except DataQualityError:
        logger.exception("数据质检阻断, 流程终止")
        sys.exit(2)
    except Exception:
        logger.exception("每日流程执行失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
