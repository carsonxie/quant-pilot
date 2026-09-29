"""Phase 1.1 诊断页面数据层 (P11-M5; 只读装配, 不 import streamlit, 可单测).

职责:
  - K 线数据: 全历史计算 MA/布林后裁剪到近六个月显示 (显示范围与计算历史分离,
    spec §8.4.7: "六个月图不能截短 MA200");
  - 列表装配: 观察名单/持仓 + 最近快照 → 分数/状态/变化/主要原因/完整度/日期;
  - 持仓读取: 账户事件回放 (复用 AccountService, 只读);
  - 快照/价格读取: 只读连接 (C21, 与写进程并发安全)。

边界: 本模块不写库; 诊断运行与名单变更由 UI 层经受控入口
(`orchestration.diagnosis_service`) 执行。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from dashboard.data_loader import _read_only, _table_exists, load_symbol_names

DISPLAY_MONTHS = 6
DEFAULT_MA_WINDOWS = (20, 60, 200)
DEFAULT_BOLL = (20, 2.0)

_STANDARD_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")


# ------------------------------------------------------------------
# 时间与价格读取
# ------------------------------------------------------------------

def _days_in_month(year: int, month: int) -> int:
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (nxt - first).days


def months_before(day: date, months: int) -> date:
    """months 个自然月前的同日 (月末截断, 无 dateutil 依赖)."""
    month_index = day.month - months
    year = day.year + (month_index - 1) // 12
    month = (month_index - 1) % 12 + 1
    return date(year, month, min(day.day, _days_in_month(year, month)))


def _standardize(df: pl.DataFrame) -> pl.DataFrame:
    cols = [c for c in _STANDARD_COLUMNS if c in df.columns]
    if df.is_empty() or "date" not in cols:
        return pl.DataFrame()
    return df.select(cols).sort("date")


def read_symbol_prices(db_path: str, symbol: str) -> pl.DataFrame:
    """研究价格全历史 (池内 → ohlcv_daily 优先; 非池 → diag_price_daily); 空 → 空表."""
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = _read_only(db_path)
    try:
        if _table_exists(db, "ohlcv_daily"):
            pool_df = _standardize(db.get_ohlcv(symbols=[symbol]))
            if not pool_df.is_empty():
                return pool_df
        if _table_exists(db, "diag_price_daily"):
            return _standardize(db.get_diag_prices(symbol))
        return pl.DataFrame()
    finally:
        db.close()


def build_kline_frame(
    prices: pl.DataFrame,
    *,
    months: int = DISPLAY_MONTHS,
    ma_windows: tuple[int, ...] = DEFAULT_MA_WINDOWS,
    boll_window: int = DEFAULT_BOLL[0],
    boll_num_std: float = DEFAULT_BOLL[1],
) -> pl.DataFrame:
    """全历史计算 MA/布林后裁剪到近 `months` 个自然月 (空表 → 空表).

    均线/布林使用窗口前的完整历史计算 — 显示范围与计算历史分离;
    不把六个月前的收盘与更长历史混算, 也不因显示裁剪而丢失预热样本。
    """
    if prices.is_empty():
        return prices
    from factor_engineering.technical import calc_bollinger, calc_sma

    df = prices.sort("date")
    close = df["close"]
    cols = [calc_sma(close, int(w)).alias(f"ma{w}") for w in ma_windows]
    boll = calc_bollinger(close, int(boll_window), float(boll_num_std))
    cols += [
        boll["mid"].alias("boll_mid"),
        boll["upper"].alias("boll_upper"),
        boll["lower"].alias("boll_lower"),
    ]
    df = df.with_columns(cols)
    cutoff = months_before(df["date"].max(), months)
    return df.filter(pl.col("date") >= cutoff)


# ------------------------------------------------------------------
# 观察名单 / 持仓读取
# ------------------------------------------------------------------

def load_watchlist_rows(db_path: str) -> pl.DataFrame:
    """观察名单 (active; 只读). 列: symbol/name/exchange/sec_type/added_at/note。"""
    if not Path(db_path).exists():
        return pl.DataFrame()
    db = _read_only(db_path)
    try:
        if not _table_exists(db, "watchlist"):
            return pl.DataFrame()
        return db.get_watchlist("active")
    finally:
        db.close()


def load_holdings(
    db_path: str, account_id: str | None, settings: dict | None = None,
) -> dict:
    """账户实际 ETF 持仓 (只读; 事件回放 + 最近可用价格).

    Returns: {"ok", "reason", "mode", "rows": [{symbol,name,shares,value,
              price_date,weight}], "cash", "total"} — 无账户/无事件时 ok=False。
    """
    empty = {"ok": False, "reason": "未配置账户", "mode": None, "rows": []}
    if not account_id or not Path(db_path).exists():
        return empty
    db = _read_only(db_path)
    try:
        if not _table_exists(db, "accounts"):
            return empty
        accounts = db.get_accounts()
        if accounts.is_empty() or "account_id" not in accounts.columns:
            return {**empty, "reason": "无账户记录"}
        matched = accounts.filter(pl.col("account_id") == str(account_id))
        if matched.is_empty():
            return {**empty, "reason": f"账户 {account_id} 不存在"}
        mode = str(matched["mode"][0])

        from data_pipeline.accounting import AccountService
        from data_pipeline.market_data import PriceSeries

        try:
            state = AccountService(db).current_state(str(account_id))
        except ValueError:
            return {"ok": False, "reason": "账户无事件记录 (未导入)", "mode": mode, "rows": []}

        prices = PriceSeries.from_db(db)
        valuation_day = prices.trading_days[-1] if prices.trading_days else date.today()
        names = load_symbol_names(settings) if settings else {}

        rows: list[dict] = []
        positions_value = 0.0
        for symbol in sorted(state.lots):
            shares = state.shares_of(symbol)
            if shares <= 0:
                continue
            found = prices.close_on_or_before(symbol, valuation_day)
            value = shares * found[0] if found else None
            if value:
                positions_value += value
            rows.append({
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "shares": shares,
                "value": value,
                "price_date": found[1].isoformat() if found else None,
            })
        total = state.cash + state.receivable + positions_value
        for row in rows:
            row["weight"] = (
                row["value"] / total if row["value"] and total > 0 else None
            )
        return {
            "ok": True, "reason": None, "mode": mode, "rows": rows,
            "cash": state.cash, "total": total,
        }
    finally:
        db.close()


# ------------------------------------------------------------------
# 快照摘要与列表装配
# ------------------------------------------------------------------

_STATUS_RANK = {"防御": 4, "警戒": 3, None: 2, "正常": 1}


def load_snapshot_details(db_path: str, symbols: list[str]) -> dict[str, dict]:
    """每个标的最近/次近快照 (解析 JSON; 无 → 空)."""
    if not symbols or not Path(db_path).exists():
        return {}
    from orchestration.diagnosis_service import get_snapshot_history

    db = _read_only(db_path)
    try:
        if not _table_exists(db, "diagnostic_snapshots"):
            return {}
        out: dict[str, dict] = {}
        for symbol in symbols:
            history = get_snapshot_history(db, symbol, limit=2)
            out[symbol] = {
                "latest": history[0] if history else None,
                "previous": history[1] if len(history) > 1 else None,
            }
        return out
    finally:
        db.close()


def _main_reason(latest: dict) -> str:
    checks = latest.get("checks") or {}
    metrics = latest.get("metrics") or {}
    missing = checks.get("missing_required") or []
    if missing:
        return str(missing[0])
    hints = metrics.get("hints") or []
    if hints:
        return str(hints[0])
    if latest.get("status"):
        return f"状态 {latest['status']} (无触发项)"
    data_status = metrics.get("data_status")
    return f"数据状态 {data_status}" if data_status else "无法评估"


def build_list_rows(entries: list[dict], snapshot_details: dict[str, dict]) -> pl.DataFrame:
    """装配诊断列表行并排序 (防御>警戒>不可评估>正常>未评估; 同档分数降序).

    entries: [{"symbol", "name", "source" ("观察"/"持仓"), "weight"?}]
    列: 名称/代码/来源/权重/分数/状态/较上次变化/主要原因/完整度/数据日期。
    """
    rows: list[dict] = []
    for entry in entries:
        symbol = str(entry["symbol"])
        details = snapshot_details.get(symbol) or {}
        latest: dict | None = details.get("latest")
        previous: dict | None = details.get("previous")

        score = status = change = reason = completeness = data_date = None
        rank = 0
        if latest is not None:
            score = latest.get("score")
            status = latest.get("status")
            completeness = latest.get("completeness")
            data_date = str(latest.get("as_of")) if latest.get("as_of") else None
            reason = _main_reason(latest)
            trigger = latest.get("trigger")
            if previous is None:
                change = "首次"
            elif previous.get("status") != status:
                prev_label = previous.get("status") or "不可评估"
                new_label = status or "不可评估"
                change = f"{prev_label} → {new_label}"
            else:
                change = str(trigger or "维持")
            rank = _STATUS_RANK.get(status, 2)

        weight = entry.get("weight")
        rows.append({
            "名称": entry.get("name") or symbol,
            "代码": symbol,
            "来源": entry.get("source"),
            "权重": f"{weight:.1%}" if isinstance(weight, (int, float)) else None,
            "分数": float(score) if score is not None else None,
            "状态": status,
            "较上次变化": change,
            "主要原因": reason,
            "完整度": completeness,
            "数据日期": data_date,
            "_rank": rank,
        })
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows)
    return df.sort(["_rank", "分数"], descending=[True, True]).drop("_rank")
