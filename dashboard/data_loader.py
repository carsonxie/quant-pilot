"""信号面板数据层 (T0.10 / P0-D10) — 从 DuckDB 组装 Streamlit 页面数据.

与 UI 解耦 (不 import streamlit), 全部为可单测的纯数据函数:
  - load_latest_signals: 最新信号日全部信号 (trade_signals)
  - build_signal_table: 今日信号展示表 (中文列)
  - build_position_view: 建议目标持仓 (target_weight > 0, 仓位降序)
  - build_price_curve: 近 N 交易日收盘价归一化宽表 (每标的首日 = 100)
  - load_macro_history: 近 N 交易日宏观得分逐日截断重算 (无状态设计, EV-009)

Usage:
    from dashboard.data_loader import load_latest_signals
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from strategies.macro_timing import MacroTiming

BENCHMARK_SYMBOL = "510300"   # 沪深300ETF, 宏观趋势维度基准 (同 daily_pipeline)
MACRO_WARMUP_DAYS = 80        # 重算窗口的 MA60 前史缓冲 (交易日)


# ------------------------------------------------------------------
# 信号与持仓
# ------------------------------------------------------------------

def load_latest_signals(db_path: str) -> pl.DataFrame:
    """读取最新信号日的全部信号 (列同 trade_signals 表).

    库文件不存在或无信号时返回空表 (不创建库文件).
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = DuckDBManager(db_path)
    try:
        return db.get_latest_signals()
    finally:
        db.close()


def load_symbol_names(settings: dict) -> dict[str, str]:
    """从 settings.universe 构建 symbol → 名称映射."""
    names: dict[str, str] = {}
    for category in settings.get("universe", {}).values():
        if isinstance(category, list):
            for etf in category:
                names[etf["symbol"]] = etf.get("name", etf["symbol"])
    return names


def universe_symbols(settings: dict) -> list[str]:
    """settings.universe 中的全部标的代码."""
    return [
        etf["symbol"]
        for category in settings.get("universe", {}).values()
        if isinstance(category, list)
        for etf in category
    ]


def build_signal_table(signals: pl.DataFrame, names: dict[str, str]) -> pl.DataFrame:
    """今日信号展示表 (中文列, 权转为百分比字符串)."""
    if signals.is_empty():
        return pl.DataFrame()
    df = signals.sort("symbol")
    return pl.DataFrame({
        "代码": df["symbol"],
        "名称": [names.get(s, s) for s in df["symbol"]],
        "方向": df["direction"],
        "目标仓位": [f"{w:.1%}" for w in df["target_weight"]],
        "触发原因": df["trigger_reason"],
    })


def build_position_view(signals: pl.DataFrame, names: dict[str, str]) -> pl.DataFrame:
    """建议目标持仓: target_weight > 0 的信号, 按仓位降序.

    全部为 0 (建议空仓) 时返回空表, 由 UI 显示空仓提示.
    """
    if signals.is_empty():
        return pl.DataFrame()
    held = signals.filter(pl.col("target_weight") > 0).sort(
        "target_weight", descending=True,
    )
    if held.is_empty():
        return pl.DataFrame()
    return pl.DataFrame({
        "代码": held["symbol"],
        "名称": [names.get(s, s) for s in held["symbol"]],
        "方向": held["direction"],
        "目标仓位": [f"{w:.1%}" for w in held["target_weight"]],
    })


# ------------------------------------------------------------------
# 行情曲线
# ------------------------------------------------------------------

def build_price_curve(
    db_path: str, symbols: list[str], n_days: int = 250,
) -> pl.DataFrame:
    """近 N 交易日收盘价归一化宽表 (date + 各标的列).

    每个标的以其窗口内首个收盘价为 100 归一, 便于多标的同图对比.
    """
    if not symbols or not Path(db_path).exists():
        return pl.DataFrame()
    db = DuckDBManager(db_path)
    try:
        ohlcv = db.get_ohlcv(symbols=symbols)
    finally:
        db.close()
    if ohlcv.is_empty():
        return pl.DataFrame()

    dates = ohlcv["date"].unique().sort().to_list()
    recent_dates = dates[-n_days:]
    recent = (
        ohlcv.filter(pl.col("date").is_in(recent_dates))
        .sort(["symbol", "date"])
        .with_columns(
            (pl.col("close") / pl.col("close").first().over("symbol") * 100.0)
            .alias("norm")
        )
    )
    return recent.pivot(on="symbol", index="date", values="norm").sort("date")


# ------------------------------------------------------------------
# 宏观得分历史 (逐日截断重算)
# ------------------------------------------------------------------

def load_macro_history(
    db_path: str, settings: dict, n_days: int = 120, step: int = 1,
) -> pl.DataFrame:
    """近 N 交易日宏观得分重算 (无状态设计 EV-009).

    对每个目标日期 t 截断窗口内数据 (≤ t) 重算得分, 窗口含 MA60 前史缓冲.
    step: 采样步长 (1=逐日; 展示场景可用 5 降低耗时), 始终包含最新交易日.
    返回 DataFrame(date, composite_score, position_cap, regime); 无数据返回空表.
    """
    if not Path(db_path).exists():
        return pl.DataFrame()

    symbols = universe_symbols(settings)
    db = DuckDBManager(db_path)
    try:
        ohlcv = db.get_ohlcv(symbols=symbols)
    finally:
        db.close()
    if ohlcv.is_empty():
        return pl.DataFrame()

    macro_cfg = settings.get("macro_timing", {})
    macro = MacroTiming(
        ma_short=macro_cfg.get("ma_short", 20),
        ma_long=macro_cfg.get("ma_long", 60),
        weight_trend=macro_cfg.get("weight_trend", 0.30),
        weight_breadth=macro_cfg.get("weight_breadth", 0.25),
        plunge_threshold=macro_cfg.get("score_plunge_threshold", 30.0),
    )

    all_dates = ohlcv["date"].unique().sort().to_list()
    candidates = all_dates[-n_days:]
    target_dates = candidates[:: max(1, step)]
    if target_dates[-1] != candidates[-1]:
        target_dates.append(candidates[-1])   # 始终包含最新交易日
    window_start = all_dates[max(0, len(all_dates) - n_days - MACRO_WARMUP_DAYS)]
    data = ohlcv.filter(pl.col("date") >= window_start)

    rows = []
    for t in target_dates:
        window = data.filter(pl.col("date") <= t)
        benchmark = window.filter(pl.col("symbol") == BENCHMARK_SYMBOL)
        if benchmark.is_empty():
            benchmark = window
        score = macro.evaluate(benchmark, window)
        rows.append({
            "date": t,
            "composite_score": score.composite_score,
            "position_cap": score.position_cap,
            "regime": score.regime,
        })
    return pl.DataFrame(rows)
