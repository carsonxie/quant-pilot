"""Streamlit 单入口面板 (T0.10 / P0-D10; M6 扩展; P11-M5 双模块).

运行:
    uv run streamlit run dashboard/app.py

模块 (侧边栏切换):
  - 账户与信号 (本文件的 render_account_panel): 数据只读 (DuckDB read_only, C21),
    覆盖 宏观/信号/目标/账户/建议/净值/运行状态;
  - 单标的诊断 (dashboard/diagnostics_app): 持仓/观察/探索 与六个月日K,
    刷新与名单变更走受控写入口 (orchestration.diagnosis_service)。

UI 只负责渲染, 数据组装在 dashboard/data_loader.py 与
dashboard/diagnostics_data.py (可单测)。
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from dashboard.data_loader import (
    BENCHMARK_SYMBOL,
    build_actual_vs_target,
    build_nav_comparison,
    build_position_view,
    build_price_curve,
    build_signal_table,
    load_account_summaries,
    load_latest_run,
    load_latest_signals,
    load_macro_history,
    load_pending_proposals,
    load_symbol_names,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
MACRO_HISTORY_DAYS = 120
MACRO_HISTORY_STEP = 5
PRICE_CURVE_DAYS = 250
COMPARISON_DAYS = 500
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


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="加载账户数据...")
def _account_views(
    db_path: str, account_id: str, band_points: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summaries = load_account_summaries(db_path)
    comparison = build_nav_comparison(db_path)
    actual_vs_target = build_actual_vs_target(
        db_path, account_id or None, band_points=band_points,
    )
    pending = load_pending_proposals(db_path, account_id or None)
    return (
        summaries.to_pandas() if not summaries.is_empty() else pd.DataFrame(),
        comparison.to_pandas() if not comparison.is_empty() else pd.DataFrame(),
        actual_vs_target.to_pandas() if not actual_vs_target.is_empty() else pd.DataFrame(),
        pending.to_pandas() if not pending.is_empty() else pd.DataFrame(),
    )


def render_account_panel(settings: dict, db_path: str) -> None:
    """账户与信号视图 (原单页内容; P11-M5 起作为入口模块之一)."""
    rebalancing = settings.get("rebalancing", {}) or {}
    account_id = str(rebalancing.get("account") or "")
    band_points = float(rebalancing.get("band_points", 0.05))

    # ---- 运行状态 (数据问题/推送结果先展示; 失败与阻断可见) ----
    latest_run = load_latest_run(db_path)
    if latest_run:
        run_label = (
            f"{latest_run['run_type']} {latest_run['run_date']} · 状态 "
            f"{latest_run['status']}"
        )
        push = (
            "推送 " + ("成功" if latest_run["push_ok"] else "未成功/未尝试")
            if latest_run["push_ok"] is not None else "推送 未记录"
        )
        st.caption(
            f"最近运行: {run_label} · 数据截止 {latest_run['data_version']} · {push} "
            f"· 运行模式 {latest_run['mode'] or '-'}"
        )
        if latest_run["status"] in ("failed", "blocked"):
            st.error(
                f"最近一次运行未通过: {latest_run['status']} — "
                f"{(latest_run.get('detail') or {}).get('error') or latest_run['push_error'] or ''}"
            )

    # ---- 今日信号 (页面主数据) ----
    signals = load_latest_signals(db_path)
    if signals.is_empty():
        st.warning("暂无交易信号 — 请先运行 `uv run daily` 生成信号。")
        st.stop()

    names = load_symbol_names(settings)
    signal_date = str(signals["signal_date"].max())

    # ---- 模拟状态声明 (C03/A0-6: 目标非成交; 真实账户录入后仍不混同) ----
    mode_note = (
        f"已配置账户 {account_id}: 建议按账户实际生成, 仍需人工执行并反馈。"
        if account_id else
        "未配置账户 (rebalancing.account 为空): 以下仅为策略目标, 非成交/持仓。"
    )
    st.warning(
        "🧪 研究/模拟模式：" + mode_note
        + " 模拟账户 (B0/B1) 的净值独立记录, 不与真实资金合并。"
    )

    # ---- 宏观状态卡 ----
    macro_df = _macro_history(db_path, MACRO_HISTORY_DAYS)
    if not macro_df.empty:
        latest = macro_df.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("宏观得分", f"{latest['composite_score']:.0f} / 100")
        c2.metric("趋势状态(510300)", REGIME_CN.get(latest["regime"], latest["regime"]))
        c3.metric("舱位上限", f"{latest['position_cap']:.0%}")
        breadth = latest.get("breadth_pct")
        if breadth is not None and breadth == breadth:  # NaN 防护
            c4.metric("ETF代理宽度", f"{breadth:.0%}")

    sig_versions = (
        sorted({v for v in signals["strategy_version"].to_list() if v})
        if "strategy_version" in signals.columns
        else []
    )
    version_label = "、".join(sig_versions) if sig_versions else str(
        settings.get("project", {}).get("strategy_version", "-")
    )
    st.caption(
        f"数据截止 {signal_date} · 信号策略版本 {version_label} · 运行模式 research · "
        f"数据源 `{settings['storage']['db_path']}` · 信号非成交（人工审核后手动执行）"
    )

    # ---- 今日信号表 (含触发原因) ----
    st.subheader("今日信号")
    signal_table = build_signal_table(signals, names)
    if not signal_table.is_empty():
        st.dataframe(signal_table.to_pandas(), width="stretch", hide_index=True)

    # ---- 策略目标持仓 (模拟) ----
    st.subheader("策略目标持仓（模拟，非实际持仓）")
    positions = build_position_view(signals, names)
    if positions.is_empty():
        st.info("当前建议空仓（全部标的的目标仓位为 0，受宏观仓位上限约束）")
    else:
        st.dataframe(positions.to_pandas(), width="stretch", hide_index=True)

    # ---- 账户视图 (M6: 实际 vs 目标 / 对照 / 待处理建议) ----
    summaries, comparison, actual_vs_target, pending = _account_views(
        db_path, account_id, band_points,
    )

    st.subheader("实际 vs 目标（账户录入后）")
    if not account_id:
        st.info(
            "未配置账户: 仅显示目标权重。录入账户: "
            "`uv run account-import --account REAL-1 --mode real --csv events.csv`, "
            "并把账户 ID 写入 settings.rebalancing.account。"
        )
    if not actual_vs_target.empty:
        st.dataframe(actual_vs_target, width="stretch", hide_index=True)

    st.subheader("待处理建议与反馈（人工执行后请录入反馈）")
    if pending.empty:
        st.info("无待处理建议（未生成或均已完成/替代/过期）。")
    else:
        st.dataframe(pending, width="stretch", hide_index=True)
        st.caption(
            "反馈通道: 成交/部分成交/拒绝/延迟/未执行按建议 ID 录入执行反馈；"
            "直接记账户事件与反馈入账二选一（避免重复计账）。"
        )

    st.subheader(f"净值对照（各账户独立；首日 = 100，仅显示近 {COMPARISON_DAYS} 日）")
    if comparison.empty:
        st.info(
            "暂无账户净值。运行基准模拟: `uv run baseline --strategy B0 --write-account` "
            "（B0/B1 为研究示例配置，不构成真实资金）。"
        )
    else:
        chart = comparison.tail(COMPARISON_DAYS).set_index("date")
        st.line_chart(chart)
        if not summaries.empty:
            st.dataframe(summaries, width="stretch", hide_index=True)
        st.caption(
            "收益口径: 现金流口径（入金不计收益）；日线数据的现金流时点按日终近似，"
            "明细见绩效报告说明。"
        )

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


def main() -> None:
    st.set_page_config(page_title="Quant-Pilot", layout="wide")
    st.title("📊 Quant-Pilot")
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    # 库路径覆盖 (测试/演练副本; 默认沿用 settings.storage.db_path)
    db_path = os.environ.get("QUANT_PILOT_DB") or str(
        PROJECT_ROOT / settings["storage"]["db_path"]
    )
    module = st.sidebar.radio(
        "功能模块", ["账户与信号", "单标的诊断"], key="app_module",
    )
    if module == "单标的诊断":
        from dashboard.diagnostics_app import render_diagnostics

        render_diagnostics(settings, db_path)
    else:
        render_account_panel(settings, db_path)


if __name__ == "__main__":
    main()
