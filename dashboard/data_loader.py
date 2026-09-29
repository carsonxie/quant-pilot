"""信号面板数据层 (T0.10 / P0-D10; M6 扩展) — 从 DuckDB 组装 Streamlit 页面数据.

与 UI 解耦 (不 import streamlit), 全部为可单测的纯数据函数:
  - load_latest_signals: 最新信号日全部信号 (trade_signals)
  - build_signal_table: 今日信号展示表 (中文列)
  - build_position_view: 建议目标持仓 (target_weight > 0, 仓位降序)
  - build_price_curve: 近 N 交易日收盘价归一化宽表 (每标的首日 = 100)
  - load_macro_history: 近 N 交易日宏观得分逐日截断重算 (无状态设计, EV-009)
  - load_account_summaries / build_nav_comparison: 真实/模拟账户净值对照 (M6-04)
  - build_actual_vs_target: 实际 vs 目标权重与偏离 (M6-04)
  - load_pending_proposals: 待处理建议与反馈状态 (M6-04)
  - load_latest_run: 最近运行状态与推送结果 (数据问题可见, M6-04)

连接约定 (C21): 面板使用只读连接 (read_only=True), 与日任务/导入的写进程并发安全;
库文件不存在时返回空表 (不创建库文件)。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from strategies.macro_timing import MacroTiming

BENCHMARK_SYMBOL = "510300"   # 沪深300ETF, 宏观趋势维度基准 (同 daily_pipeline)
MACRO_WARMUP_DAYS = 80        # 重算窗口的 MA60 前史缓冲 (交易日)


def _read_only(db_path: str) -> DuckDBManager:
    """只读连接 (C21: 面板不写库; 与写进程并发安全)."""
    manager = DuckDBManager(db_path)
    if not Path(db_path).exists():
        raise FileNotFoundError(db_path)
    manager._conn = duckdb.connect(str(db_path), read_only=True)
    return manager


def _table_exists(db: DuckDBManager, name: str) -> bool:
    row = db.conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = ? AND table_schema = 'main'",
        [name],
    ).fetchone()
    return bool(row and row[0] > 0)


# ------------------------------------------------------------------
# 信号与持仓
# ------------------------------------------------------------------

def load_latest_signals(db_path: str) -> pl.DataFrame:
    """读取最新信号日的全部信号 (列同 trade_signals 表).

    库文件不存在或无信号时返回空表 (不创建库文件; 只读连接)。
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = _read_only(db_path)
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
    """今日信号展示表 (中文列, 权转为百分比字符串; 含策略版本列)."""
    if signals.is_empty():
        return pl.DataFrame()
    df = signals.sort("symbol")
    versions = (
        [v if v is not None else "-" for v in df["strategy_version"].to_list()]
        if "strategy_version" in df.columns
        else ["-"] * df.height
    )
    return pl.DataFrame({
        "代码": df["symbol"],
        "名称": [names.get(s, s) for s in df["symbol"]],
        "方向": df["direction"],
        "目标仓位": [f"{w:.1%}" for w in df["target_weight"]],
        "版本": versions,
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
    db = _read_only(db_path)
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
    db = _read_only(db_path)
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
            "breadth_pct": score.breadth_pct,
        })
    return pl.DataFrame(rows)


# ------------------------------------------------------------------
# M6: 账户净值对照 / 实际 vs 目标 / 待处理建议 / 运行状态
# ------------------------------------------------------------------

MODE_CN = {
    "real": "真实", "paper": "模拟(纸上)", "backtest": "模拟(回测)",
    "research": "研究", "unknown": "未知",
}


def load_account_summaries(db_path: str) -> pl.DataFrame:
    """全部账户绩效摘要 (真实/模拟独立, 不合并; spec §3.1).

    列: 账户/模式/期初/期末/期间收益/链接TWR/投资盈亏/净外部入金。
    无账户或无事件 → 空表。
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    from backtest.performance import performance_for_account

    db = _read_only(db_path)
    try:
        if not _table_exists(db, "accounts"):
            return pl.DataFrame()
        accounts = db.get_accounts()
        if accounts.is_empty() or "account_id" not in accounts.columns:
            return pl.DataFrame()
        rows: list[dict] = []
        for account_id in accounts["account_id"].to_list():
            try:
                report = performance_for_account(db, str(account_id))
            except ValueError:
                continue  # 无事件账户
            rows.append({
                "账户": str(account_id),
                "模式": MODE_CN.get(report.mode, report.mode),
                "区间": f"{report.start} → {report.end}",
                "期初": round(report.nav_start, 2),
                "期末": round(report.nav_end, 2),
                "期间收益": f"{report.simple_return():.2%}",
                "链接TWR": f"{report.twr():.2%}",
                "投资盈亏": round(report.investment_pnl, 2),
                "净外部入金": round(report.net_external_flow, 2),
            })
        return pl.DataFrame(rows)
    finally:
        db.close()


def build_nav_comparison(db_path: str, account_ids: list[str] | None = None) -> pl.DataFrame:
    """账户净值归一化对照 (首日 = 100; 各账户独立, 不相加).

    account_ids=None → 全部有事件账户。
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    from backtest.performance import compare_accounts

    db = _read_only(db_path)
    try:
        if not _table_exists(db, "accounts"):
            return pl.DataFrame()
        if account_ids is None:
            accounts = db.get_accounts()
            if accounts.is_empty() or "account_id" not in accounts.columns:
                return pl.DataFrame()
            account_ids = [str(a) for a in accounts["account_id"].to_list()]
        if not account_ids:
            return pl.DataFrame()
        _summaries, wide = compare_accounts(db, list(account_ids))
        return wide
    finally:
        db.close()


def build_actual_vs_target(
    db_path: str,
    account_id: str | None,
    band_points: float | None = None,
) -> pl.DataFrame:
    """实际 vs 目标权重与偏离 (spec §6.5: 目标列表不得充当真实持仓).

    - 无账户或未指定 → 返回空表 (UI 显示"未录入账户")。
    - 目标来自最近写入的 target 记录 (daily), 实际来自账户事件回放。
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = _read_only(db_path)
    try:
        if not _table_exists(db, "targets") or not _table_exists(db, "accounts"):
            return pl.DataFrame()
        target_row = db.conn.execute(
            "SELECT target_id, as_of FROM targets ORDER BY as_of DESC, created_at DESC "
            "LIMIT 1"
        ).fetchone()
        if target_row is None:
            return pl.DataFrame()
        target_id, as_of = target_row[0], target_row[1]
        weights = db.get_target_weights(target_id)
        targets = {
            str(row["symbol"]): float(row["target_weight"])
            for row in weights.iter_rows(named=True)
        } if not weights.is_empty() else {}

        actual: dict[str, float] = {}
        if account_id:
            from data_pipeline.accounting import AccountService
            from data_pipeline.market_data import PriceSeries

            try:
                state = AccountService(db).current_state(account_id)
                prices = PriceSeries.from_db(db)
                valuation_day = as_of if as_of is not None else date.today()
                positions: dict[str, float] = {}
                positions_value = 0.0
                for sym in state.lots:
                    shares = state.shares_of(sym)
                    if shares <= 0:
                        continue
                    found = prices.close_on_or_before(sym, valuation_day)
                    if found is None:
                        continue
                    value = shares * found[0]
                    positions[sym] = value
                    positions_value += value
                total = state.cash + state.receivable + positions_value
                if total > 0:
                    actual = {s: v / total for s, v in positions.items()}
            except ValueError:
                pass

        symbols = sorted((set(targets) | set(actual)) - {"__CASH__"})
        rows = []
        band = band_points if band_points is not None else 0.05
        for symbol in symbols:
            target_weight = float(targets.get(symbol, 0.0))
            actual_weight = float(actual.get(symbol, 0.0))
            deviation = actual_weight - target_weight
            flag = "越界(需调整)" if abs(deviation) >= band - 1e-12 else "带内"
            rows.append({
                "代码": symbol,
                "目标权重": f"{target_weight:.1%}",
                "实际权重": f"{actual_weight:.1%}" if account_id else "-",
                "偏离": f"{deviation:+.1%}" if account_id else "-",
                "状态": flag if account_id else "无账户",
            })
        if "__CASH__" in targets:
            rows.append({
                "代码": "__CASH__",
                "目标权重": f"{float(targets['__CASH__']):.1%}",
                "实际权重": "-",
                "偏离": "-",
                "状态": "—",
            })
        table = pl.DataFrame(rows)
        if not table.is_empty():
            table = table.with_columns(pl.lit(str(as_of)).alias("目标日期"))
        return table
    finally:
        db.close()


def load_pending_proposals(db_path: str, account_id: str | None = None) -> pl.DataFrame:
    """待处理建议 (proposed / partially_filled) 与最近反馈状态 (M6-04).

    列: 建议/标的/方向/份额/参考价/目标|实际权重/优先级/依赖/有效期/状态/原因/反馈。
    """
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = _read_only(db_path)
    try:
        if not _table_exists(db, "trade_proposals"):
            return pl.DataFrame()
        query = (
            "SELECT proposal_id, symbol, side, shares, ref_price, target_weight, "
            "actual_weight, priority, depends_on, valid_until, status, reason "
            "FROM trade_proposals WHERE status IN ('proposed', 'partially_filled')"
        )
        params: list = []
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        query += " ORDER BY priority DESC, symbol"
        result = db.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()

        feedback_by_proposal: dict[str, str] = {}
        if _table_exists(db, "execution_feedback"):
            feedback_rows = db.conn.execute(
                "SELECT proposal_id, status, COUNT(*) FROM execution_feedback "
                "WHERE proposal_id IS NOT NULL GROUP BY proposal_id, status"
            ).fetchall()
            for proposal_id, status, count in feedback_rows:
                feedback_by_proposal[str(proposal_id)] = f"{status}×{count}"

        rows = []
        for (proposal_id, symbol, side, shares, ref_price, target_weight,
             actual_weight, priority, depends_on, valid_until, status, reason) in result:
            rows.append({
                "建议": str(proposal_id)[:12],
                "代码": symbol,
                "方向": side,
                "份额": int(shares) if shares is not None else None,
                "参考价": ref_price,
                "目标权重": (
                    f"{target_weight:.1%}" if target_weight is not None else "-"
                ),
                "实际权重": (
                    f"{actual_weight:.1%}" if actual_weight is not None else "-"
                ),
                "优先级": priority,
                "依赖": depends_on or "-",
                "有效期至": str(valid_until) if valid_until is not None else "-",
                "状态": status,
                "反馈": feedback_by_proposal.get(str(proposal_id), "—"),
                "原因": reason,
            })
        return pl.DataFrame(rows)
    finally:
        db.close()


def load_latest_run(db_path: str) -> dict | None:
    """最近运行记录 (状态/数据版本/推送结果; 失败与阻断可见, M6-04)."""
    if not Path(db_path).exists():
        return None
    db = _read_only(db_path)
    try:
        if not _table_exists(db, "runs"):
            return None
        row = db.conn.execute(
            "SELECT run_id, run_type, run_date, mode, strategy_version, "
            "data_version, status, push_ok, push_attempts, push_error, "
            "detail_json, finished_at FROM runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        columns = [
            "run_id", "run_type", "run_date", "mode", "strategy_version",
            "data_version", "status", "push_ok", "push_attempts", "push_error",
            "detail_json", "finished_at",
        ]
        info = dict(zip(columns, row, strict=True))
        try:
            info["detail"] = json.loads(info["detail_json"] or "{}")
        except (TypeError, ValueError):
            info["detail"] = {}
        return info
    finally:
        db.close()
