"""自研轻量回测循环 v0.1 (T0.9) — T+0/T+1 混合撮合的现实主义回测引擎.

核心约束 (AGENTS §5.2 / background.md 致命坑):
  - 无未来函数: T 日收盘后生成信号, 一律 T+1 开盘价成交 (禁止 T 日收盘价成交)
  - 摩擦成本: 单边佣金 0.075% (round-trip 0.15%, 用户确认口径) +
    每笔滑点 = max(0.15%, 成交金额 / 20日均成交额 × 0.5%)。
    口径说明 (spec v3.0 §6.3, C08): 该滑点公式为**明确命名的旧压力情景**
    ("legacy_stress"), 不是经校准的市场冲击定律; 实验对照情景经 cost= 参数注入。
  - 最低收费: 每笔佣金 = max(成交额 × 费率, min_commission_per_trade)
  - 批次与现金 (C05): 买入按批次 (lot) 记录可卖时间; 卖出款当日可用于买入,
    T+1 才可取; nav 同时记录 cash 与 cash_withdrawable
  - T+0/T+1 元数据路由: 买入份额按 settlement_type 解锁
    (T+0 当日可卖; T+1 次日可卖); 锁定期卖单顺延至解锁日开盘执行
  - 动态仓位上限: 逐日读取 position_cap (来自宏观择时), 约束目标总市值
  - 最小交易单位 100 份; 卖出先于买入 (A股卖出资金当日可用)
  - 每日净值记录 (date, cash, position_value, total_nav)

Usage:
    uv run backtest   # 510300 买入持有 + T+0/T+1 交收对比 (真实数据)

    或代码调用:
    result = run_custom_backtest(
        ohlcv, target_weights,
        settlement={"510300": "T+1", "513100": "T+0"},
        initial_cash=100_000.0,
        settings=settings,
    )
    print(result.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import polars as pl

logger = logging.getLogger(__name__)

# ---- 常量 ----
LOT_SIZE = 100                 # ETF 最小交易单位 (份, 1 手)
TRADING_DAYS_PER_YEAR = 252

# settings.trading_cost 缺省值 (与 config/settings.yaml 保持一致)
_DEFAULT_COST = {
    "commission_rate": 0.00075,          # 单边佣金 (round-trip 0.15%, 研究假设)
    "min_commission_per_trade": 0.0,     # 最低佣金/笔 (max(费率×金额, 本值))
    "slippage_base": 0.0015,             # 滑点下限 0.15%/笔 (旧压力情景参数)
    "impact_coefficient": 0.005,         # 冲击项: 金额/日均额 × 0.5% (旧压力情景参数)
    "slippage_lookback_days": 20,
}


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class Trade:
    """单笔成交记录."""

    trade_date: date
    symbol: str
    side: str              # BUY / SELL
    shares: int
    price: float           # 含滑点后的实际成交价
    amount: float          # 实际成交金额 (price × shares)
    commission: float      # 佣金
    slippage_cost: float   # 相对无滑点价格的额外成本
    settlement_date: date  # 买入: 份额可卖起始日; 卖出: 资金可用日 (当日)
    note: str = ""


@dataclass
class _Lot:
    """买入批次 (C05: 批次级可卖时间)."""

    shares: int
    unlock_date: date   # 份额可卖起始日 (T+0 = 成交当日; T+1 = 次一交易日)


@dataclass
class _Position:
    """单标的持仓状态 (按买入批次管理可卖时间)."""

    lots: list[_Lot] = field(default_factory=list)
    cost_basis: float = 0.0                 # 累计买入成本 (含费用, 供诊断)

    @property
    def shares(self) -> int:
        return sum(lot.shares for lot in self.lots)

    def available(self, d: date) -> int:
        """d 日可卖份额 (解锁日 ≤ d 的批次合计)."""
        return sum(lot.shares for lot in self.lots if lot.unlock_date <= d)

    def locked_unlock_date(self, d: date) -> date | None:
        """d 日仍锁定批次的最早解锁日 (无则 None)."""
        dates = [lot.unlock_date for lot in self.lots if lot.unlock_date > d]
        return min(dates) if dates else None

    def consume_available(self, d: date, n: int) -> int:
        """从可卖批次 FIFO 扣减 n 份, 返回实际扣减数."""
        consumed = 0
        for lot in self.lots:
            if consumed >= n:
                break
            if lot.unlock_date <= d and lot.shares > 0:
                take = min(lot.shares, n - consumed)
                lot.shares -= take
                consumed += take
        self.lots = [lot for lot in self.lots if lot.shares > 0]
        return consumed


# 价格/权益口径说明 (C09/A0-4): 当前使用不复权价格, 不含分红/权益事件
CALIBER_NOTE = "价格口径: 不复权 (Sina); 不含分红/权益事件 — 完整账本验收待 M2"


@dataclass
class BacktestResult:
    """回测结果."""

    nav: pl.DataFrame          # date, cash, cash_withdrawable, position_value, nav, cap
    trades: list[Trade]
    initial_cash: float
    metrics: dict[str, float] = field(default_factory=dict)
    caliber_note: str = CALIBER_NOTE

    def summary(self) -> str:
        """人类可读摘要."""
        m = self.metrics
        lines = [
            "BacktestResult("
            f"days={m.get('n_trading_days', 0):.0f}, "
            f"total_return={m.get('total_return', 0.0):.2%}, "
            f"annual_return={m.get('annual_return', 0.0):.2%}, "
            f"max_drawdown={m.get('max_drawdown', 0.0):.2%}, "
            f"trades={m.get('n_trades', 0):.0f}, "
            f"total_cost={m.get('total_cost', 0.0):,.2f})",
            f"口径: {self.caliber_note}",
        ]
        return "\n".join(lines)


# ------------------------------------------------------------------
# 引擎
# ------------------------------------------------------------------

class CustomLoopBacktest:
    """日线事件循环回测引擎 (纯 Python, 100 只标的无性能瓶颈).

    撮合时序 (每日):
      1. 开盘: 解锁到期份额 → 执行顺延卖单 → 执行昨日收盘生成的调仓指令
      2. 收盘: 按当日收盘价估值, 记录净值
    """

    def __init__(
        self,
        initial_cash: float,
        settlement: dict[str, str],
        cost: dict | None = None,
        position_cap: float | dict[date, float] | None = None,
    ) -> None:
        """初始化引擎.

        Args:
            initial_cash: 初始资金 (元)
            settlement: symbol → "T+0" | "T+1" (来自 DuckDB etf_metadata)
            cost: trading_cost 配置 (缺省用 settings.yaml 同款默认值)
            position_cap: 动态仓位上限; None=100%; float=常量; dict=逐日映射
        """
        if initial_cash <= 0:
            raise ValueError(f"initial_cash 必须为正: {initial_cash}")

        self.initial_cash = float(initial_cash)
        self.settlement = {s: str(t).upper() for s, t in settlement.items()}
        self.cost = {**_DEFAULT_COST, **(cost or {})}
        self.position_cap = position_cap

        # 运行时状态
        self.cash = float(initial_cash)                # 可用现金 (卖出款当日可用于买入)
        self.cash_withdrawable = float(initial_cash)   # 可取现金 (卖出款 T+1 可取, C05)
        self.pending_withdrawable = 0.0                # 当日卖出待转可取的金额
        self.positions: dict[str, _Position] = {}
        self.trades: list[Trade] = []
        self.pending_sells: list[dict] = []
        self.nav_records: list[dict] = []
        self.last_nav = float(initial_cash)
        self.last_close: dict[str, float] = {}

        # 市场数据 (run 时准备)
        self.dates: list[date] = []
        self._date_index: dict[date, int] = {}
        self.opens: dict[str, dict[date, float]] = {}
        self.closes: dict[str, dict[date, float]] = {}
        self.avg_amounts: dict[str, dict[date, float]] = {}

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run(
        self,
        ohlcv: pl.DataFrame,
        target_weights: pl.DataFrame,
    ) -> BacktestResult:
        """执行回测.

        Args:
            ohlcv: 行情; 必需列 symbol, date, open, close, amount
            target_weights: 每日收盘后的目标权重 (date, symbol, weight);
                date = 信号日, 在下一交易日开盘执行; 缺省日期 = 维持原目标

        Returns:
            BacktestResult
        """
        self._prepare_market_data(ohlcv)
        weights_by_date = self._prepare_weights(target_weights)

        for i, d in enumerate(self.dates):
            # ---- 开盘撮合 ----
            self._open_session(d)
            if i > 0:
                signal_date = self.dates[i - 1]
                if signal_date in weights_by_date:
                    self._rebalance_to_target(weights_by_date[signal_date], d)

            # ---- 收盘估值 ----
            position_value = self._mark_to_market(d)
            total_nav = self.cash + position_value
            self.last_nav = total_nav
            self.nav_records.append({
                "date": d,
                "cash": self.cash,
                "cash_withdrawable": self.cash_withdrawable,
                "position_value": position_value,
                "total_nav": total_nav,
                "position_cap": self._cap_for(d),
            })

        nav_df = pl.DataFrame(self.nav_records)
        metrics = self._compute_metrics(nav_df)
        self._log_trade_summary()
        return BacktestResult(
            nav=nav_df,
            trades=list(self.trades),
            initial_cash=self.initial_cash,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def _prepare_market_data(self, ohlcv: pl.DataFrame) -> None:
        required = {"symbol", "date", "open", "close", "amount"}
        missing = required - set(ohlcv.columns)
        if missing:
            raise ValueError(
                f"ohlcv 缺少必需列: {sorted(missing)}. 现有: {sorted(ohlcv.columns)}"
            )
        if ohlcv.is_empty():
            raise ValueError("ohlcv 为空, 无法回测")
        if ohlcv["open"].min() <= 0 or ohlcv["close"].min() <= 0:
            raise ValueError("存在非正价格, 拒绝回测 (数据质检未通过?)")

        lookback = int(self.cost["slippage_lookback_days"])
        df = (
            ohlcv.select(["symbol", "date", "open", "close", "amount"])
            .sort(["symbol", "date"])
            .with_columns(
                # 截至 T-1 的 20 日均成交额 (shift 防未来函数)
                pl.col("amount")
                .rolling_mean(window_size=lookback, min_samples=1)
                .shift(1)
                .over("symbol")
                .alias("avg_amount")
            )
        )

        self.dates = df["date"].unique().sort().to_list()
        self._date_index = {d: i for i, d in enumerate(self.dates)}

        for sym in df["symbol"].unique().to_list():
            sub = df.filter(pl.col("symbol") == sym)
            self.opens[sym] = dict(
                zip(sub["date"].to_list(), sub["open"].to_list(), strict=True)
            )
            self.closes[sym] = dict(
                zip(sub["date"].to_list(), sub["close"].to_list(), strict=True)
            )
            self.avg_amounts[sym] = {
                d: float(a)
                for d, a in zip(
                    sub["date"].to_list(), sub["avg_amount"].to_list(), strict=True
                )
                if a is not None
            }

    def _prepare_weights(self, target_weights: pl.DataFrame) -> dict[date, dict[str, float]]:
        required = {"date", "symbol", "weight"}
        missing = required - set(target_weights.columns)
        if missing:
            raise ValueError(f"target_weights 缺少必需列: {sorted(missing)}")

        weights: dict[date, dict[str, float]] = {}
        for row in target_weights.iter_rows(named=True):
            if float(row["weight"]) < 0:
                raise ValueError(f"目标权重不可为负: {row}")
            weights.setdefault(row["date"], {})[row["symbol"]] = float(row["weight"])
        return weights

    def _next_trading_day(self, d: date) -> date:
        """d 的下一交易日 (用于 T+1 解锁日)."""
        idx = self._date_index[d]
        if idx + 1 >= len(self.dates):
            return d  # 已是最后一日, 无后续交易日 (调用方无实际影响)
        return self.dates[idx + 1]

    # ------------------------------------------------------------------
    # 开盘撮合
    # ------------------------------------------------------------------

    def _open_session(self, d: date) -> None:
        """开盘阶段: 前日卖出款转可取 → 执行顺延卖单 (lot 解锁按日期自动生效)."""
        # 1. 前一日卖出资金 T+1 转为可取 (C05: 可取 ≠ 当日可用)
        if self.pending_withdrawable > 0:
            self.cash_withdrawable += self.pending_withdrawable
            self.pending_withdrawable = 0.0

        # 2. 顺延卖单 (锁定期间被推迟的卖出)
        deferred: list[dict] = []
        for order in self.pending_sells:
            if order["not_before"] <= d:
                self._execute_sell(
                    order["symbol"], order["shares"], d, note="锁定顺延成交",
                )
            else:
                deferred.append(order)
        self.pending_sells = deferred

    def _rebalance_to_target(self, target: dict[str, float], d: date) -> None:
        """将组合调整至目标权重 (以开盘价成交, 卖出先于买入)."""
        nav_base = self.last_nav
        cap = self._cap_for(d)

        sum_w = sum(w for w in target.values() if w > 0)
        scale = 0.0 if sum_w <= 0 else min(1.0, cap / sum_w)

        # 目标份数 (整手截断)
        target_shares: dict[str, int] = {}
        for sym, w in target.items():
            if w <= 0:
                continue
            price = self.opens.get(sym, {}).get(d)
            if price is None or price <= 0:
                logger.warning("%s 在 %s 无开盘价, 跳过调仓", sym, d)
                continue
            target_value = nav_base * w * scale
            target_shares[sym] = int(target_value / price / LOT_SIZE) * LOT_SIZE

        all_syms = sorted(set(self.positions) | set(target_shares))

        # 卖出优先 (释放资金; A股卖出资金当日可用)
        for sym in all_syms:
            cur = self.positions[sym].shares if sym in self.positions else 0
            tgt = target_shares.get(sym, 0)
            if tgt < cur:
                self._execute_sell(sym, cur - tgt, d, note="调仓卖出")

        # 再买入
        for sym in all_syms:
            cur = self.positions[sym].shares if sym in self.positions else 0
            tgt = target_shares.get(sym, 0)
            if tgt > cur:
                self._execute_buy(sym, tgt - cur, d, note="调仓买入")

    def _commission_of(self, amount: float) -> float:
        """每笔佣金 = max(成交额 × 单边费率, 最低收费) (spec §6.3, C08)."""
        rate = float(self.cost["commission_rate"])
        minimum = float(self.cost.get("min_commission_per_trade", 0.0))
        return max(amount * rate, minimum)

    def _slippage_rate(self, symbol: str, d: date, amount_est: float) -> float:
        """每笔滑点率 = max(下限, 交易金额 / 前日为止20日均额 × 冲击系数)."""
        base = float(self.cost["slippage_base"])
        avg = self.avg_amounts.get(symbol, {}).get(d)
        if avg is None or avg <= 0:
            return base
        impact = amount_est / avg * float(self.cost["impact_coefficient"])
        return max(base, impact)

    def _execute_sell(
        self, symbol: str, shares_want: int, d: date, note: str = "",
    ) -> Trade | None:
        """卖出; 锁定份额顺延. 返回实际成交记录 (无可卖时 None)."""
        pos = self.positions.get(symbol)
        price0 = self.opens.get(symbol, {}).get(d)
        if pos is None or pos.shares <= 0 or shares_want <= 0 or price0 is None:
            return None

        # 批次级可卖约束 (C05): 只卖解锁日 ≤ d 的份额
        exec_shares = min(shares_want, pos.available(d))

        trade: Trade | None = None
        if exec_shares > 0:
            amount_est = price0 * exec_shares
            slip = self._slippage_rate(symbol, d, amount_est)
            price = price0 * (1.0 - slip)
            amount = price * exec_shares
            commission = self._commission_of(amount)
            self.cash += amount - commission
            self.pending_withdrawable += amount - commission  # 卖出款 T+1 才可取 (C05)
            pos.consume_available(d, exec_shares)
            trade = Trade(
                trade_date=d, symbol=symbol, side="SELL", shares=exec_shares,
                price=price, amount=amount, commission=commission,
                slippage_cost=(price0 - price) * exec_shares,
                settlement_date=d,  # 卖出资金当日可用 (可取为 T+1)
                note=note,
            )
            self.trades.append(trade)

        # 锁定份额 → 顺延至该批次解锁日
        deferred = shares_want - exec_shares
        if deferred > 0:
            locked = pos.shares - pos.available(d)
            unlock_date = pos.locked_unlock_date(d)
            if locked > 0 and unlock_date is not None:
                self.pending_sells.append({
                    "symbol": symbol,
                    "shares": min(deferred, locked),
                    "not_before": unlock_date,
                })
                logger.warning(
                    "%s: 卖单 %d 股中 %d 股锁定 (最早解锁日 %s), 已顺延",
                    symbol, shares_want, deferred, unlock_date,
                )
            else:
                logger.warning(
                    "%s: 卖单超过总持仓, 丢弃 %d 股", symbol, deferred,
                )
        return trade

    def _execute_buy(
        self, symbol: str, shares_want: int, d: date, note: str = "",
    ) -> Trade | None:
        """买入; 受现金与整手约束. 返回成交记录 (未成交时 None)."""
        price0 = self.opens.get(symbol, {}).get(d)
        if shares_want <= 0 or price0 is None:
            return None

        amount_est = price0 * shares_want
        slip = self._slippage_rate(symbol, d, amount_est)
        price = price0 * (1.0 + slip)
        commission_rate = float(self.cost["commission_rate"])

        # 现金约束 (含佣金)
        unit_cost = price * (1.0 + commission_rate)
        max_affordable = int(self.cash / unit_cost / LOT_SIZE) * LOT_SIZE
        exec_shares = min(shares_want, max_affordable)
        if exec_shares <= 0:
            logger.warning("%s: 现金不足, 无法买入 %d 股", symbol, shares_want)
            return None
        if exec_shares < shares_want:
            logger.warning(
                "%s: 现金不足, 买入缩减 %d → %d 股",
                symbol, shares_want, exec_shares,
            )

        amount = price * exec_shares
        commission = self._commission_of(amount)
        self.cash -= amount + commission

        # 交收: T+0 当日可卖; T+1 次日解锁
        settlement_type = self.settlement.get(symbol, "T+1")
        unlock_date = d if settlement_type == "T+0" else self._next_trading_day(d)

        pos = self.positions.setdefault(symbol, _Position())
        pos.lots.append(_Lot(shares=exec_shares, unlock_date=unlock_date))
        pos.cost_basis += amount + commission
        # 可取现金不得超过可用现金 (买入花费后收紧; 当日卖出款不可取)
        self.cash_withdrawable = max(0.0, min(self.cash_withdrawable, self.cash))

        trade = Trade(
            trade_date=d, symbol=symbol, side="BUY", shares=exec_shares,
            price=price, amount=amount, commission=commission,
            slippage_cost=(price - price0) * exec_shares,
            settlement_date=unlock_date,
            note=note,
        )
        self.trades.append(trade)
        return trade

    # ------------------------------------------------------------------
    # 估值与指标
    # ------------------------------------------------------------------

    def _mark_to_market(self, d: date) -> float:
        """按当日收盘价估值持仓 (停牌日沿用最近价)."""
        value = 0.0
        for sym, pos in self.positions.items():
            if pos.shares <= 0:
                continue
            close = self.closes.get(sym, {}).get(d)
            if close is None:
                close = self.last_close.get(sym)
            else:
                self.last_close[sym] = close
            if close is None:
                logger.warning("%s 在 %s 前无收盘价, 按 0 估值", sym, d)
                continue
            value += pos.shares * close
        return value

    def _cap_for(self, d: date) -> float:
        """取 d 日生效的仓位上限 (dict 取最近一个 <= d 的值)."""
        cap = self.position_cap
        if cap is None:
            return 1.0
        if isinstance(cap, dict):
            if not cap:
                return 1.0
            eligible = [k for k in cap if k <= d]
            if eligible:
                return float(cap[max(eligible)])
            return float(cap[min(cap)])
        return float(cap)

    def _compute_metrics(self, nav_df: pl.DataFrame) -> dict[str, float]:
        n_days = nav_df.height
        nav_start = self.initial_cash
        nav_end = float(nav_df["total_nav"][-1])
        total_return = nav_end / nav_start - 1.0

        years = max(n_days - 1, 1) / TRADING_DAYS_PER_YEAR
        annual_return = (nav_end / nav_start) ** (1.0 / years) - 1.0

        running_max = nav_df["total_nav"].cum_max()
        drawdown = 1.0 - nav_df["total_nav"] / running_max
        max_drawdown = float(drawdown.max())

        total_commission = sum(t.commission for t in self.trades)
        total_slippage = sum(t.slippage_cost for t in self.trades)

        return {
            "n_trading_days": float(n_days),
            "final_nav": nav_end,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "n_trades": float(len(self.trades)),
            "total_commission": total_commission,
            "total_slippage": total_slippage,
            "total_cost": total_commission + total_slippage,
        }

    def _log_trade_summary(self) -> None:
        m = self._compute_metrics(pl.DataFrame(self.nav_records))
        logger.info(
            "回测完成: %d 交易日, 总收益 %.2f%%, 年化 %.2f%%, 最大回撤 %.2f%%, "
            "%d 笔成交, 摩擦成本 %.2f 元 (佣金 %.2f + 滑点 %.2f)",
            int(m["n_trading_days"]), m["total_return"] * 100,
            m["annual_return"] * 100, m["max_drawdown"] * 100,
            int(m["n_trades"]), m["total_cost"],
            m["total_commission"], m["total_slippage"],
        )


# ------------------------------------------------------------------
# 便捷入口
# ------------------------------------------------------------------

def buy_and_hold_targets(ohlcv: pl.DataFrame, symbol: str) -> pl.DataFrame:
    """构造买入持有目标权重: 首个交易日收盘后 100% 持有该标的."""
    first_date = ohlcv["date"].min()
    return pl.DataFrame({
        "date": [first_date],
        "symbol": [symbol],
        "weight": [1.0],
    })


def run_custom_backtest(
    ohlcv: pl.DataFrame,
    target_weights: pl.DataFrame,
    *,
    settlement: dict[str, str],
    initial_cash: float,
    settings: dict | None = None,
    position_cap: float | dict[date, float] | None = None,
) -> BacktestResult:
    """回测入口 (自研轻量循环 v0.1).

    Args:
        ohlcv: 行情 (symbol, date, open, close, amount; 建议全列)
        target_weights: 目标权重 (date=信号日, symbol, weight)
        settlement: symbol → "T+0" | "T+1"
        initial_cash: 初始资金
        settings: 完整 settings 字典; 取 trading_cost 段 (缺省用内置默认)
        position_cap: 动态仓位上限 (None=100%)

    Returns:
        BacktestResult
    """
    cost = (settings or {}).get("trading_cost", {})
    engine = CustomLoopBacktest(
        initial_cash=initial_cash,
        settlement=settlement,
        cost=cost,
        position_cap=position_cap,
    )
    return engine.run(ohlcv, target_weights)


# ------------------------------------------------------------------
# CLI 入口 (uv run backtest)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 510300 买入持有 + T+0/T+1 交收对比 (真实数据)."""
    import sys
    from pathlib import Path

    import yaml

    from data_pipeline.duckdb_manager import DuckDBManager

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parents[2]
    with open(project_root / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    db = DuckDBManager(str(project_root / settings["storage"]["db_path"]))
    try:
        initial_cash = float(settings.get("backtest", {}).get("initial_cash", 100_000.0))
        cost_scenario = settings.get("trading_cost", {}).get("scenario", "legacy_stress")
        print("==== Quant-Pilot 自研回测 v0.2 ====")
        print(f"初始资金: {initial_cash:,.0f} 元")
        print(f"成本情景: {cost_scenario} (旧压力情景, 未校准 — spec §6.3)")
        print(f"口径提示: {CALIBER_NOTE}")

        for symbol in ("510300", "513100"):
            df = db.get_ohlcv(symbols=[symbol])
            if df.is_empty():
                print(f"\n[{symbol}] 无数据, 跳过")
                continue

            meta = db.get_etf_metadata(symbols=[symbol])
            settlement_type = "T+1"
            if not meta.is_empty() and "settlement_type" in meta.columns:
                settlement_type = str(meta["settlement_type"][0])

            targets = buy_and_hold_targets(df, symbol)
            result = run_custom_backtest(
                df, targets,
                settlement={symbol: settlement_type},
                initial_cash=initial_cash,
                settings=settings,
            )

            # 纯价格基准 (同口径: 信号次日开盘买入 → 期末收盘, 无摩擦)
            dates = sorted(df["date"].to_list())
            entry_date = dates[1] if len(dates) > 1 else dates[0]
            entry_price = float(df.filter(pl.col("date") == entry_date)["open"][0])
            exit_price = float(df.filter(pl.col("date") == dates[-1])["close"][0])
            years = max(len(dates) - 1, 1) / TRADING_DAYS_PER_YEAR
            bench_annual = (exit_price / entry_price) ** (1.0 / years) - 1.0

            m = result.metrics
            gap = abs(m["annual_return"] - bench_annual)
            print(f"\n[{symbol} 买入持有] {dates[0]} → {dates[-1]} "
                  f"({years:.1f} 年, {settlement_type})")
            print(f"  期末净值: {m['final_nav']:,.2f} 元 | 总收益 {m['total_return']:.2%} "
                  f"| 年化 {m['annual_return']:.2%}")
            print(f"  纯价格基准年化: {bench_annual:.2%} | 偏差 {gap:.3%} (验收 < 1%)")
            print(f"  成交 {int(m['n_trades'])} 笔 | 摩擦成本 {m['total_cost']:,.2f} 元 "
                  f"(佣金 {m['total_commission']:,.2f} + 滑点 {m['total_slippage']:,.2f})")
            if result.trades:
                t = result.trades[0]
                print(
                    f"  首笔买入 {t.trade_date} {t.shares:,} 股 @ {t.price:.4f} "
                    f"→ 份额可卖日 {t.settlement_date}"
                )

        # ---- T+0/T+1 交收对比 (共同交易日窗口, 验收: 差一个交易日) ----
        print("\n==== T+0/T+1 交收对比 (近 250 个共同交易日) ====")
        frames = {
            s: db.get_ohlcv(symbols=[s]).sort("date") for s in ("510300", "513100")
        }
        common_dates = sorted(
            set(frames["510300"]["date"].to_list())
            & set(frames["513100"]["date"].to_list())
        )
        info: dict[str, Trade] = {}
        for symbol, stype in (("510300", "T+1"), ("513100", "T+0")):
            recent = (
                frames[symbol]
                .filter(pl.col("date").is_in(common_dates))
                .tail(250)
            )
            res = run_custom_backtest(
                recent, buy_and_hold_targets(recent, symbol),
                settlement={symbol: stype},
                initial_cash=initial_cash, settings=settings,
            )
            t = res.trades[0]
            info[symbol] = t
            print(f"  {symbol} ({stype}): 成交 {t.trade_date} → 份额可卖 {t.settlement_date}")

        idx = {d: i for i, d in enumerate(common_dates)}
        diff = idx[info["510300"].settlement_date] - idx[info["513100"].settlement_date]
        mark = "✓" if diff == 1 else "✗"
        print(f"  交收日期相差 {diff} 个交易日 (验收: 1) {mark}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
