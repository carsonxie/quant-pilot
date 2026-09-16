"""持仓状态化止损与重新进入 (M4-06, C06) — 持有期高水位 + 棘轮 + 冷却.

契约 (spec §5.5):
  - 统一价格口径 (不复权收盘价, 与回测/信号一致), 高水位只在**实际持有期间**
    维护 (entry_date 起), 不再用固定 2×ATR 回看窗口近似 (旧近似已由本模块替换).
  - 止损线 = high_water − N×ATR, 只按规定上移 (棘轮, 不因 ATR 波动下移).
  - 用 T 日收盘价与当时有效止损线比较; 触发后最早下一可交易时段执行
    (成交价不假设为止损价, 见 custom_loop 时序约定).
  - 重新进入 (spec §5.5): 退出已反馈 → 有一个后续决策日 → 退出条件解除 →
    冷却满足 → 新一轮选择仍合格; 重新建仓时高水位与止损状态按规则重置.
  - 无法卖出 (停牌/未解锁) 时持续展示剩余风险, 不从账本删除持仓.
  - 会话间状态持久化: strategy_states 表 (schema v5).

状态字段 (strategy_states 表):
  high_water / atr / stop_price   — 止损状态; pending_exit + exit_reason
  — 已触发但尚未卖出的退出指令 (每轮持续提示, 直至份额归零);
  exit_date/exit_reason/cooldown_until — 退出与冷却; 冷却按自然日近似标注.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import date, timedelta

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from strategies.risk_manager import RiskManager

logger = logging.getLogger(__name__)

POSITION_STATE_SCHEMA = {
    "symbol": pl.Utf8,
    "has_position": pl.Boolean,
    "entry_date": pl.Date,
    "high_water": pl.Float64,
    "atr": pl.Float64,
    "stop_price": pl.Float64,
    "stop_triggered": pl.Boolean,
    "pending_exit": pl.Boolean,
    "exit_reason": pl.Utf8,
    "exit_date": pl.Date,
    "cooldown_until": pl.Date,
    "updated_at": pl.Datetime("us"),
}


@dataclass
class PositionState:
    """单标的持有期状态 (策略层, 与账户账本分离)."""

    symbol: str
    has_position: bool = False
    entry_date: date | None = None
    high_water: float | None = None
    atr: float | None = None
    stop_price: float | None = None
    stop_triggered: bool = False
    pending_exit: bool = False          # 已触发退出, 等待卖出 (每轮持续提示)
    exit_reason: str | None = None
    exit_date: date | None = None
    cooldown_until: date | None = None

    def cooldown_note(self, as_of: date) -> str | None:
        """冷却说明 (None = 无冷却)."""
        if self.exit_date is None:
            return None
        if self.cooldown_until is not None and as_of < self.cooldown_until:
            return (
                f"止损退出 {self.exit_date} ({self.exit_reason or '-'}), "
                f"冷却至 {self.cooldown_until} (spec §5.5 重新进入)"
            )
        return None


def load_position_states(db: DuckDBManager) -> dict[str, PositionState]:
    """读取全部标的状态 (无表/无行 → 空字典)."""
    if not db.table_exists("strategy_states"):
        return {}
    df = db.get_strategy_states()
    if df.is_empty():
        return {}
    states: dict[str, PositionState] = {}
    for row in df.iter_rows(named=True):
        states[str(row["symbol"])] = PositionState(
            symbol=str(row["symbol"]),
            has_position=bool(row.get("has_position")),
            entry_date=row.get("entry_date"),
            high_water=row.get("high_water"),
            atr=row.get("atr"),
            stop_price=row.get("stop_price"),
            stop_triggered=bool(row.get("stop_triggered")),
            pending_exit=bool(row.get("pending_exit")),
            exit_reason=row.get("exit_reason"),
            exit_date=row.get("exit_date"),
            cooldown_until=row.get("cooldown_until"),
        )
    return states


def persist_position_states(
    db: DuckDBManager, states: dict[str, PositionState],
) -> int:
    """写回状态 (幂等 UPSERT; 只保留持仓/冷却/未完成退出相关行)."""
    rows = [
        {
            "symbol": state.symbol,
            "has_position": state.has_position,
            "entry_date": state.entry_date,
            "high_water": state.high_water,
            "atr": state.atr,
            "stop_price": state.stop_price,
            "stop_triggered": state.stop_triggered,
            "pending_exit": state.pending_exit,
            "exit_reason": state.exit_reason,
            "exit_date": state.exit_date,
            "cooldown_until": state.cooldown_until,
            "updated_at": None,
        }
        for state in states.values()
    ]
    df = pl.DataFrame(rows, schema=POSITION_STATE_SCHEMA) if rows else pl.DataFrame(
        schema=POSITION_STATE_SCHEMA,
    )
    return db.upsert_strategy_states(df)


def update_position_states(
    states: dict[str, PositionState],
    rm: RiskManager,
    ohlcv: pl.DataFrame,
    held_symbols: set[str],
    entry_dates: dict[str, date],
    as_of: date,
    cooldown_days: int,
) -> tuple[dict[str, PositionState], list[str], dict[str, str]]:
    """每日更新: 高水位/ATR/止损线 (棘轮) → 触发判定 → 冷却推进.

    Args:
        states: 现有状态 (load_position_states; 会被复制更新)
        rm: 风控器 (提供 ATR 参数与计算)
        ohlcv: 行情 (含 symbol/date/high/low/close)
        held_symbols: 当前实际持仓标的 (来自账户账本, 权威)
        entry_dates: 持仓建立日期 (批次最早 open_date; 无则取最早可用行情日)
        as_of: 决策日 (T 日)
        cooldown_days: 止损退出后的冷却天数 (自然日近似, 标注)

    Returns:
        (更新后状态, 本轮止损触发标的列表, 冷却中标的 → 说明)
    """
    updated = dict(states)
    triggered: list[str] = []

    # 1) 逐标的更新持有期状态
    for symbol in sorted(held_symbols):
        sym_df = (
            ohlcv.filter(pl.col("symbol") == symbol).sort("date")
            if not ohlcv.is_empty() else pl.DataFrame()
        )
        if sym_df.is_empty():
            logger.warning("%s: 无行情, 跳过持有期状态更新", symbol)
            continue
        closes = [
            (row_date, float(row_close))
            for row_date, row_close in zip(
                sym_df["date"].to_list(), sym_df["close"].to_list(), strict=True,
            )
            if row_date is not None and row_close is not None
        ]
        if not closes:
            continue
        last_date, last_close = closes[-1]
        if last_date > as_of:
            closes = [(d, c) for d, c in closes if d <= as_of]
            if not closes:
                continue
            last_date, last_close = closes[-1]

        entry = entry_dates.get(symbol)
        if entry is None:
            entry = closes[0][0]
        prev = updated.get(symbol)
        if prev is None or not prev.has_position:
            # 重新建仓: 高水位与止损状态重置 (spec §5.5)
            prev = PositionState(
                symbol=symbol, has_position=True, entry_date=entry,
                high_water=None, stop_price=None, stop_triggered=False,
            )
        elif prev.entry_date is None or entry > prev.entry_date:
            # 批次全部换新 → 视作重新建仓 (重置高水位)
            prev = dataclass_replace(
                prev, entry_date=entry, high_water=None, stop_price=None,
            )

        series = rm.calc_atr_series(sym_df)
        atr = None
        if series is not None:
            idx = len(series) - 1 - (len(sym_df) - len(closes))  # 对齐 ≤ as_of
            idx = max(0, min(idx, len(series) - 1))
            value = series[idx]
            if value is not None and value == value:  # 非 nan
                atr = float(value)

        # 持有期高水位 (实际持有期间, 不是固定回看窗口)
        window_high = max(
            (c for d, c in closes if prev.entry_date is None or d >= prev.entry_date),
            default=last_close,
        )
        high_water = max(window_high, prev.high_water or window_high)

        if prev.pending_exit:
            # 退出已触发但未卖出: 保留指令与原因, 继续更新 ATR (展示距离)
            state = dataclass_replace(
                prev, has_position=True, atr=atr,
                high_water=high_water,
            )
            updated[symbol] = state
            triggered.append(symbol)
            continue

        if atr is not None and atr > 0:
            base_stop = max(0.0, high_water - rm.atr_multiplier * atr)
            stop = RiskManager.enforce_stop_ratchet(base_stop, prev.stop_price)
        else:
            stop = prev.stop_price
        is_triggered = bool(stop is not None and last_close <= stop + 1e-12)

        if is_triggered:
            reason = (
                f"ATR止损触发: 收盘 {last_close:.3f} ≤ 止损 {stop:.3f} "
                f"(高水位 {high_water:.3f}, ATR {atr if atr is not None else float('nan'):.4f})"
            )
            state = PositionState(
                symbol=symbol, has_position=True, entry_date=prev.entry_date,
                high_water=high_water, atr=atr, stop_price=stop,
                stop_triggered=True, pending_exit=True, exit_reason=reason,
                exit_date=as_of,
                cooldown_until=as_of + timedelta(days=max(0, cooldown_days)),
            )
            updated[symbol] = state
            triggered.append(symbol)
            logger.warning("%s: %s", symbol, reason)
        else:
            updated[symbol] = PositionState(
                symbol=symbol, has_position=True, entry_date=prev.entry_date,
                high_water=high_water, atr=atr, stop_price=stop,
                stop_triggered=False, pending_exit=False,
                exit_reason=prev.exit_reason, exit_date=prev.exit_date,
                cooldown_until=prev.cooldown_until,
            )

    # 2) 非持仓标的: 完成退出 → 进入冷却; 冷却到期后清理记录
    for symbol in list(updated):
        state = updated[symbol]
        if symbol in held_symbols:
            continue
        if state.pending_exit or state.stop_triggered:
            # 退出已完成 (份额归零): 记录退出与冷却 (退出条件解除需重新评估
            # 由重新进入检查负责; 冷却起点 = 退出执行后的确认日)
            exit_date = state.exit_date or as_of
            updated[symbol] = PositionState(
                symbol=symbol, has_position=False, entry_date=None,
                high_water=None, atr=state.atr, stop_price=None,
                stop_triggered=False, pending_exit=False,
                exit_reason=state.exit_reason, exit_date=exit_date,
                cooldown_until=exit_date + timedelta(days=max(0, cooldown_days)),
            )
        elif not state.has_position and state.exit_date is not None:
            if state.cooldown_until is None or as_of >= state.cooldown_until:
                del updated[symbol]  # 冷却结束且无持仓: 清理

    return updated, triggered, cooldown_symbols(updated, as_of)


def cooldown_symbols(
    states: dict[str, PositionState], as_of: date,
) -> dict[str, str]:
    """冷却中标的 → 说明 (供调仓规划器禁止新买, spec §5.5)."""
    result: dict[str, str] = {}
    for symbol, state in states.items():
        if state.has_position:
            continue
        note = state.cooldown_note(as_of)
        if note:
            result[symbol] = note
    return result


def stop_distance_display(
    states: dict[str, PositionState],
    ohlcv: pl.DataFrame,
    held_symbols: set[str],
    as_of: date,
) -> list[str]:
    """持仓止损距离展示 (含停牌无法卖出时的持续风险提示)."""
    lines: list[str] = []
    for symbol in sorted(held_symbols):
        state = states.get(symbol)
        if state is None or state.stop_price is None:
            lines.append(f"  {symbol}: 止损状态未知 (数据不足)")
            continue
        sym_df = ohlcv.filter(pl.col("symbol") == symbol).sort("date")
        if sym_df.is_empty():
            lines.append(f"  {symbol}: 无行情 — 停牌/数据缺失, 风险持续展示")
            continue
        last_close = float(sym_df["close"][-1])
        distance = (last_close - state.stop_price) / last_close if last_close else 0.0
        flag = " [已触发待执行]" if state.pending_exit else ""
        lines.append(
            f"  {symbol}: close={last_close:.3f} stop={state.stop_price:.3f} "
            f"dist={distance:.1%} trail_high={state.high_water:.3f}{flag}"
        )
    return lines
