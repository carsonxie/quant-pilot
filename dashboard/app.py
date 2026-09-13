"""Streamlit 单页面信号面板 (T0.10 / P0-D10).

运行:
    uv run streamlit run dashboard/app.py

数据只读 (DuckDB trade_signals + ohlcv_daily), 不写库.
UI 只负责渲染, 数据组装全部在 dashboard/data_loader.py (可单测).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from dashboard.data_loader import (
    BENCHMARK_SYMBOL,
    build_position_view,
    build_price_curve,
    build_signal_table,
    load_latest_signals,
    load_macro_history,
    load_symbol_names,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
MACRO_HISTORY_DAYS = 120
MACRO_HISTORY_STEP = 5
PRICE_CURVE_DAYS = 250
CACHE_TTL_SECONDS = 300
REGIME_CN = {"bull": "多头", "bear": "空头", "ranging": "震荡"}


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="加载宏观数据...")
def _macro_history(db_path: str, n_days: int) -> pd.DataFrame:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    df = load_macro_history(
        db_path, settings, n_days=n_days, step=MACRO_HISTORY_STEP,
    )
    return df.to_pandas() if not df.is_empty() else pd.DataFrame()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="加载行情数据...")
def _price_curve(db_path: str, symbols: tuple[str, ...], n_days: int) -> pd.DataFrame:
    df = build_price_curve(db_path, list(symbols), n_days=n_days)
    return df.to_pandas() if not df.is_empty() else pd.DataFrame()


def main() -> None:
    st.set_page_config(page_title="Quant-Pilot 信号面板", layout="wide")
    st.title("📊 Quant-Pilot 信号面板")

    with open(SETTINGS_PATH, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    db_path = str(PROJECT_ROOT / settings["storage"]["db_path"])

    # ---- 今日信号 (页面主数据) ----
    signals = load_latest_signals(db_path)
    if signals.is_empty():
        st.warning("暂无交易信号 — 请先运行 `uv run daily` 生成信号。")
        st.stop()

    names = load_symbol_names(settings)
    signal_date = str(signals["signal_date"].max())

    # ---- 宏观状态卡 ----
    macro_df = _macro_history(db_path, MACRO_HISTORY_DAYS)
    if not macro_df.empty:
        latest = macro_df.iloc[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("宏观得分", f"{latest['composite_score']:.0f} / 100")
        c2.metric("市场状态", REGIME_CN.get(latest["regime"], latest["regime"]))
        c3.metric("仓位上限", f"{latest['position_cap']:.0%}")

    st.caption(
        f"信号日期 {signal_date} · 数据源 `{settings['storage']['db_path']}` · "
        "信号仅供参考，人工审核后手动执行"
    )

    # ---- 今日信号表 (含触发原因) ----
    st.subheader("今日信号")
    signal_table = build_signal_table(signals, names)
    if not signal_table.is_empty():
        st.dataframe(signal_table.to_pandas(), width="stretch", hide_index=True)

    # ---- 建议目标持仓 ----
    st.subheader("建议目标持仓")
    positions = build_position_view(signals, names)
    if positions.is_empty():
        st.info("当前建议空仓（全部标的的目标仓位为 0，受宏观仓位上限约束）")
    else:
        st.dataframe(positions.to_pandas(), width="stretch", hide_index=True)

    # ---- 近期走势 ----
    st.subheader(f"近期走势（近 {PRICE_CURVE_DAYS} 交易日，首日归一 = 100）")
    if not positions.is_empty():
        chart_symbols = tuple(positions["代码"].to_list())
        chart_caption = "建议持仓标的归一化收盘价"
    else:
        chart_symbols = (BENCHMARK_SYMBOL,)
        chart_caption = f"当前建议空仓，展示基准 {BENCHMARK_SYMBOL} 沪深300ETF"
    curve = _price_curve(db_path, chart_symbols, PRICE_CURVE_DAYS)
    if curve.empty:
        st.info("暂无行情数据")
    else:
        st.caption(chart_caption)
        st.line_chart(curve.set_index("date"))

    # ---- 宏观得分趋势 ----
    st.subheader(f"宏观得分趋势（近 {MACRO_HISTORY_DAYS} 交易日）")
    if macro_df.empty:
        st.info("暂无宏观数据")
    else:
        st.line_chart(macro_df.set_index("date")["composite_score"])


if __name__ == "__main__":
    main()
