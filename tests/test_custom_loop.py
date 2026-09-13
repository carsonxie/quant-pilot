"""自研回测循环 (T0.9) 单元与集成测试.

覆盖:
  - 撮合时序: T 日收盘信号 → T+1 开盘成交 (无未来函数)
  - 摩擦成本: 单边佣金 0.075% (round-trip 0.15%) + 滑点 max(0.15%, 金额/20日均额×0.5%)
  - T+0/T+1: 交收日相差一个交易日; 锁定份额卖单顺延
  - 动态仓位上限 (常量 / 逐日映射); 现金约束; 整手截断; 每日净值记录
  - 真实数据集成 (本地 DuckDB 存在时): 510300 买入持有 vs 纯价格基准 < 1%
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml

from backtest.custom_loop import (
    LOT_SIZE,
    CustomLoopBacktest,
    _Lot,
    _Position,
    buy_and_hold_targets,
    run_custom_backtest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "quant_pilot.duckdb"
INITIAL_CASH = 100_000.0
COMMISSION_RATE = 0.00075   # 单边
SLIPPAGE_BASE = 0.0015      # 0.15%/笔


# ------------------------------------------------------------------
# 合成数据
# ------------------------------------------------------------------

def business_days(start: date, n: int) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def make_ohlcv(
    symbols: list[str],
    n_days: int = 6,
    start: date = date(2026, 1, 5),
    prices: dict[str, list[float]] | None = None,
    amount: float = 1e9,
) -> pl.DataFrame:
    """合成行情: 恒定价格 (默认 10.0) 或按给定价格路径 (open=close=path)."""
    days = business_days(start, n_days)
    rows = []
    for sym in symbols:
        path = (prices or {}).get(sym) or [10.0] * n_days
        assert len(path) == n_days, f"{sym} 价格路径长度须为 {n_days}"
        for d, p in zip(days, path, strict=True):
            rows.append({
                "symbol": sym, "date": d, "open": p, "high": p, "low": p,
                "close": p, "volume": 1_000_000.0, "amount": amount,
            })
    return pl.DataFrame(rows)


def weights(rows: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": [r[0] for r in rows], "symbol": [r[1] for r in rows],
         "weight": [r[2] for r in rows]}
    )


class TestMatchingTiming:
    """T 日收盘信号 → T+1 开盘价成交 (禁止 T 日收盘价, AGENTS §8 #2)."""

    def test_signal_executes_at_next_open(self):
        df = make_ohlcv(["510300"], n_days=6, prices={"510300": [10, 11, 12, 13, 14, 15]})
        days = business_days(date(2026, 1, 5), 6)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )

        assert len(result.trades) == 1
        t = result.trades[0]
        # 成交在信号日次日, 价格 = 次日开盘 × (1 + 滑点下限)
        assert t.trade_date == days[1]
        assert t.price == pytest.approx(11.0 * (1 + SLIPPAGE_BASE))

    def test_no_trade_on_signal_day(self):
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )

        assert all(t.trade_date > days[0] for t in result.trades)

    def test_future_data_does_not_change_past_execution(self):
        """篡改信号日之后的价格, T+1 成交价与我方净值不受影响."""
        days = business_days(date(2026, 1, 5), 6)
        base = make_ohlcv(["510300"], n_days=6, prices={"510300": [10, 11, 12, 13, 14, 15]})
        tampered = base.with_columns(
            pl.when(pl.col("date") >= days[2]).then(pl.col("open") * 5.0)
            .otherwise(pl.col("open")).alias("open")
        )
        targets = weights([(days[0], "510300", 1.0)])

        r_base = run_custom_backtest(base, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})
        r_tampered = run_custom_backtest(tampered, targets, settlement={"510300": "T+1"},
                                         initial_cash=INITIAL_CASH, settings={})

        # 成交 (发生在 days[1]) 不受 days[2] 之后数据影响
        assert r_base.trades[0].price == pytest.approx(r_tampered.trades[0].price)
        assert r_base.trades[0].shares == r_tampered.trades[0].shares
        # days[1] 收盘净值一致
        nav_base = r_base.nav.filter(pl.col("date") == days[1])["total_nav"][0]
        nav_tamp = r_tampered.nav.filter(pl.col("date") == days[1])["total_nav"][0]
        assert nav_base == pytest.approx(nav_tamp)


class TestFrictionCosts:
    """佣金 round-trip 0.15% + 滑点 = max(0.15%, 金额/20日均额×0.5%)."""

    def test_commission_charged_both_sides(self):
        df = make_ohlcv(["510300"], n_days=6)  # 恒定 10.0
        days = business_days(date(2026, 1, 5), 6)
        targets = weights([(days[0], "510300", 1.0), (days[2], "510300", 0.0)])  # 买后清仓

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )

        buys = [t for t in result.trades if t.side == "BUY"]
        sells = [t for t in result.trades if t.side == "SELL"]
        assert len(buys) == 1 and len(sells) == 1

        for t in result.trades:
            assert t.commission == pytest.approx(t.amount * COMMISSION_RATE)

        # 佣金: 每笔 = 成交额 × 0.075%, 买卖两端各收一次 (round-trip 0.15% 语义)
        total_commission = sum(t.commission for t in result.trades)
        traded_amount = sum(t.amount for t in result.trades)
        assert total_commission == pytest.approx(traded_amount * COMMISSION_RATE, rel=0.01)
        m = result.metrics
        assert m["total_cost"] == pytest.approx(m["total_commission"] + m["total_slippage"])

    def test_slippage_base_floor_with_liquid_symbol(self):
        """日均额巨大 → 滑点 = 下限 0.15%."""
        df = make_ohlcv(["510300"], n_days=4, amount=1e9)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})
        t = result.trades[0]
        assert t.slippage_cost == pytest.approx(t.amount / (1 + SLIPPAGE_BASE) * SLIPPAGE_BASE)

    def test_slippage_impact_scales_with_trade_size(self):
        """交易额 ≈ 日均额 → 滑点 = 金额/日均额 × 0.5% = 0.5% > 下限."""
        df = make_ohlcv(["510300"], n_days=4, amount=100_000)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})
        t = result.trades[0]
        # 信号日 w=1.0 → 目标 10000 股; est 金额 100000 / avg 100000 × 0.005 = 0.5%
        assert t.price == pytest.approx(10.0 * 1.005)
        assert t.slippage_cost == pytest.approx((t.price - 10.0) * t.shares)

    def test_cash_constraint_and_lot_size(self):
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])  # 想全仓 10000 股

        result = run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})
        t = result.trades[0]

        # 现金约束: 9900 股 (10000 股需 100225 元 > 10 万)
        assert t.shares == 9900
        assert t.shares % LOT_SIZE == 0
        final_cash = result.nav["cash"][-1]
        assert final_cash >= 0


class TestSettlement:
    """T+0/T+1 元数据路由: 交收日相差一个交易日; 锁定卖单顺延."""

    def test_settlement_dates_differ_by_one_trading_day(self):
        days = business_days(date(2026, 1, 5), 6)
        df = make_ohlcv(["510300", "513100"], n_days=6)
        targets = weights([
            (days[0], "510300", 1.0), (days[0], "513100", 1.0),
        ])

        r_t1 = run_custom_backtest(
            df, targets, settlement={"510300": "T+1", "513100": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )
        r_t0 = run_custom_backtest(
            df, targets, settlement={"510300": "T+0", "513100": "T+0"},
            initial_cash=INITIAL_CASH, settings={},
        )

        # 同等信号: T+1 标的份额次日解锁, T+0 当日解锁 → 差一个交易日
        assert r_t1.trades[0].settlement_date == days[2]
        assert r_t0.trades[0].settlement_date == days[1]
        assert r_t1.trades[0].trade_date == r_t0.trades[0].trade_date == days[1]

    def test_locked_shares_defer_sell_to_unlock_date(self):
        """白盒: 尝试卖出锁定份额 → 成交 0, 顺延至解锁日开盘执行."""
        df = make_ohlcv(["510300"], n_days=6)
        engine = CustomLoopBacktest(
            initial_cash=INITIAL_CASH, settlement={"510300": "T+1"},
        )
        engine._prepare_market_data(df)
        days = engine.dates

        # 人为构造: days[1] 买入 1000 股 (T+1 批次, 解锁日 days[2])
        engine.positions["510300"] = _Position(
            lots=[_Lot(shares=1000, unlock_date=days[2])],
        )

        trade = engine._execute_sell("510300", 1000, days[1])
        assert trade is None                       # 全部锁定, 无成交
        assert engine.positions["510300"].shares == 1000
        assert len(engine.pending_sells) == 1
        assert engine.pending_sells[0]["not_before"] == days[2]

        # days[2] 开盘: 解锁并顺延成交
        engine._open_session(days[2])
        assert engine.positions["510300"].shares == 0
        assert engine.pending_sells == []
        assert len(engine.trades) == 1
        assert engine.trades[0].note == "锁定顺延成交"
        assert engine.trades[0].trade_date == days[2]

    def test_t_plus_0_shares_sellable_same_day(self):
        """白盒: T+0 当日买入份额, 同日可卖 (无锁定)."""
        df = make_ohlcv(["513100"], n_days=4)
        engine = CustomLoopBacktest(
            initial_cash=INITIAL_CASH, settlement={"513100": "T+0"},
        )
        engine._prepare_market_data(df)
        days = engine.dates

        buy = engine._execute_buy("513100", 1000, days[1])
        assert buy is not None
        pos = engine.positions["513100"]
        assert pos.available(days[1]) == 1000              # T+0 无锁定
        assert pos.locked_unlock_date(days[1]) is None

        sell = engine._execute_sell("513100", 1000, days[1])
        assert sell is not None and sell.shares == 1000        # 同日可卖


class TestPositionCap:
    """动态仓位上限约束目标总市值."""

    def test_constant_cap_limits_exposure(self):
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={}, position_cap=0.5,
        )
        t = result.trades[0]
        assert t.shares == 5000     # 50000 元 / 10 元
        # 持仓市值 ≤ 上限
        nav_row = result.nav.filter(pl.col("date") == days[1])
        assert nav_row["position_value"][0] <= INITIAL_CASH * 0.5

    def test_dict_cap_uses_latest_effective_value(self):
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
            position_cap={days[1]: 0.2},
        )
        assert result.trades[0].shares == 2000


class TestNavRecords:
    """每日净值记录."""

    def test_nav_recorded_every_trading_day(self):
        df = make_ohlcv(["510300"], n_days=6)
        days = business_days(date(2026, 1, 5), 6)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})

        assert result.nav.height == len(days)
        assert result.nav["date"].to_list() == days
        assert set(result.nav.columns) == {
            "date", "cash", "cash_withdrawable", "position_value",
            "total_nav", "position_cap",
        }
        # 首日无成交, 净值 == 初始资金
        assert result.nav["total_nav"][0] == pytest.approx(INITIAL_CASH)
        # 记账恒等式: cash + position_value == total_nav
        for row in result.nav.iter_rows(named=True):
            assert row["cash"] + row["position_value"] == pytest.approx(row["total_nav"])

    def test_metrics_consistent(self):
        df = make_ohlcv(["510300"], n_days=30,
                        prices={"510300": [10.0 * (1.001 ** i) for i in range(30)]})
        days = business_days(date(2026, 1, 5), 30)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                     initial_cash=INITIAL_CASH, settings={})
        m = result.metrics
        assert m["n_trading_days"] == 30
        assert m["final_nav"] == pytest.approx(result.nav["total_nav"][-1])
        assert m["total_return"] == pytest.approx(m["final_nav"] / INITIAL_CASH - 1)
        assert 0.0 <= m["max_drawdown"] <= 1.0


class TestValidation:
    """输入守卫."""

    def test_missing_columns_raise(self):
        df = make_ohlcv(["510300"], n_days=4).drop("amount")
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])
        with pytest.raises(ValueError, match="缺少必需列"):
            run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                initial_cash=INITIAL_CASH, settings={})

    def test_non_positive_price_raise(self):
        df = make_ohlcv(["510300"], n_days=4, prices={"510300": [10, 10, -1, 10]})
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])
        with pytest.raises(ValueError, match="非正价格"):
            run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                initial_cash=INITIAL_CASH, settings={})

    def test_negative_weight_raise(self):
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", -0.5)])
        with pytest.raises(ValueError, match="不可为负"):
            run_custom_backtest(df, targets, settlement={"510300": "T+1"},
                                initial_cash=INITIAL_CASH, settings={})

    def test_buy_and_hold_targets_helper(self):
        df = make_ohlcv(["510300"], n_days=3)
        targets = buy_and_hold_targets(df, "510300")
        assert targets.height == 1
        assert targets["weight"][0] == 1.0


# ------------------------------------------------------------------
# 真实数据集成 (本地 DuckDB 存在时运行)
# ------------------------------------------------------------------

def _load_real_settings() -> dict:
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


requires_db = pytest.mark.skipif(not DB_PATH.exists(), reason="本地 DuckDB 不存在")


@pytest.fixture(scope="module")
def db():
    from data_pipeline.duckdb_manager import DuckDBManager

    manager = DuckDBManager(str(DB_PATH))
    yield manager
    manager.close()


@requires_db
class TestRealData:
    """T0.9 验收: 510300 买入持有 vs 纯价格基准; T+0/T+1 交收差一天."""

    def test_510300_buy_and_hold_annual_gap_under_1pct(self, db):
        settings = _load_real_settings()
        initial_cash = float(settings["backtest"]["initial_cash"])
        df = db.get_ohlcv(symbols=["510300"])
        assert not df.is_empty()

        targets = buy_and_hold_targets(df, "510300")
        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=initial_cash, settings=settings,
        )

        dates = sorted(df["date"].to_list())
        entry_price = float(df.filter(pl.col("date") == dates[1])["open"][0])
        exit_price = float(df.filter(pl.col("date") == dates[-1])["close"][0])
        years = (len(dates) - 1) / 252
        bench_annual = (exit_price / entry_price) ** (1 / years) - 1

        gap = abs(result.metrics["annual_return"] - bench_annual)
        msg = (
            f"年化偏差 {gap:.3%} ≥ 1% "
            f"(引擎年化 {result.metrics['annual_return']:.2%}, 基准 {bench_annual:.2%})"
        )
        assert gap < 0.01, msg

    def test_real_settlement_differs_one_day(self, db):
        settings = _load_real_settings()
        initial_cash = float(settings["backtest"]["initial_cash"])

        # 元数据必须与 settings 约定一致 (510300=T+1, 513100=T+0)
        for symbol, expected in (("510300", "T+1"), ("513100", "T+0")):
            meta = db.get_etf_metadata(symbols=[symbol])
            assert not meta.is_empty() and str(meta["settlement_type"][0]) == expected

        # 共同交易日窗口 (两标的上市场景不同, 取交集保证同起点同成交日)
        d300 = db.get_ohlcv(symbols=["510300"]).sort("date")
        d100 = db.get_ohlcv(symbols=["513100"]).sort("date")
        common = sorted(set(d300["date"].to_list()) & set(d100["date"].to_list()))
        if len(common) < 30:
            pytest.skip("两标的共同交易日不足 30 天")

        trades = {}
        for symbol, stype in (("510300", "T+1"), ("513100", "T+0")):
            df = db.get_ohlcv(symbols=[symbol]).sort("date")
            recent = df.filter(pl.col("date").is_in(common)).tail(200)
            result = run_custom_backtest(
                recent, buy_and_hold_targets(recent, symbol),
                settlement={symbol: stype},
                initial_cash=initial_cash, settings=settings,
            )
            trades[symbol] = result.trades[0]

        t1, t0 = trades["510300"], trades["513100"]
        assert t1.trade_date == t0.trade_date   # 同一交易日开盘买入
        idx = {d: i for i, d in enumerate(common)}
        # T+1 份额次一交易日解锁, T+0 当日解锁 → 交收日期相差一天
        assert idx[t1.settlement_date] - idx[t0.settlement_date] == 1


# ------------------------------------------------------------------
# C08: 成本情景命名 + 最低收费 + 手工往返对账 (独立期望值)
# ------------------------------------------------------------------

class TestCostScenarioAndMinFee:
    """最低收费 = max(成交额×费率, min_commission); 滑点含价不二次扣现金."""

    def test_min_commission_applied_when_above_rate(self):
        """小额交易 (2,000 元) 佣金 = max(1.5, 5.0) = 5.0 元."""
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 0.02)])  # 2,000 元 → 200 股

        cost = {
            "commission_rate": 0.00075, "min_commission_per_trade": 5.0,
            "slippage_base": 0.0, "impact_coefficient": 0.0,
            "slippage_lookback_days": 20,
        }
        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={"trading_cost": cost},
        )

        t = result.trades[0]
        assert t.shares == 200
        assert t.commission == pytest.approx(5.0)  # max(2000×0.00075=1.5, 5.0)
        # 现金独立核对: 初始 − 成交额 − 佣金
        assert result.nav["cash"][-1] == pytest.approx(INITIAL_CASH - t.amount - 5.0)

    def test_min_commission_ignored_below_rate(self):
        """大额交易 → 佣金 = 成交额 × 费率, 最低收费不生效."""
        df = make_ohlcv(["510300"], n_days=4)
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        cost = {
            "commission_rate": 0.00075, "min_commission_per_trade": 5.0,
            "slippage_base": 0.0, "impact_coefficient": 0.0,
            "slippage_lookback_days": 20,
        }
        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={"trading_cost": cost},
        )
        t = result.trades[0]
        assert t.commission == pytest.approx(t.amount * 0.00075)
        assert t.commission > 5.0

    def test_round_trip_manual_reconciliation(self):
        """手工往返对账 (独立期望值, 不复用实现输出).

        价格路径 10,10,10,12,12,12; 信号日0收盘买入 → 日1开盘成交;
        信号日2收盘清仓 → 日3开盘成交. 滑点 0.15% 含在成交价, 佣金 0.075%/边.
        手工: 买 5,000 股 @10.015 (额 50,075, 费 37.55625);
              卖 5,000 股 @11.982 (额 59,910, 费 44.9325);
              期末 = 100,000 − 50,075 − 37.55625 + 59,910 − 44.9325 = 109,752.51125.
        """
        df = make_ohlcv(
            ["510300"], n_days=6,
            prices={"510300": [10, 10, 10, 12, 12, 12]},
        )
        days = business_days(date(2026, 1, 5), 6)
        targets = weights([(days[0], "510300", 0.5), (days[2], "510300", 0.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )

        buys = [t for t in result.trades if t.side == "BUY"]
        sells = [t for t in result.trades if t.side == "SELL"]
        assert len(buys) == 1 and len(sells) == 1
        buy, sell = buys[0], sells[0]
        assert buy.trade_date == days[1] and sell.trade_date == days[3]

        assert buy.shares == 5_000 and buy.price == pytest.approx(10.015)
        assert sell.shares == 5_000 and sell.price == pytest.approx(11.982)
        assert buy.commission == pytest.approx(50_075.0 * 0.00075)
        assert sell.commission == pytest.approx(59_910.0 * 0.00075)
        assert result.metrics["final_nav"] == pytest.approx(109_752.51125, abs=0.01)

    def test_slippage_embedded_in_price_not_double_charged(self):
        """滑点已体现在成交价 → 现金流只按价格计, 不再次扣滑点."""
        df = make_ohlcv(["510300"], n_days=4, prices={"510300": [10, 10, 10, 10]})
        days = business_days(date(2026, 1, 5), 4)
        targets = weights([(days[0], "510300", 1.0)])

        result = run_custom_backtest(
            df, targets, settlement={"510300": "T+1"},
            initial_cash=INITIAL_CASH, settings={},
        )
        t = result.trades[0]
        # 现金变化 = 成交额 + 佣金 (滑点只在价格里, 不单独扣)
        assert result.nav["cash"][-1] == pytest.approx(INITIAL_CASH - t.amount - t.commission)
        assert t.slippage_cost > 0  # 滑点成本仅作诊断展示


# ------------------------------------------------------------------
# C05: 批次级可卖 + 现金可用/可取 + 不可成交场景 (独立期望值)
# ------------------------------------------------------------------

class TestLotAndCashAvailability:
    """批次可卖时间、可取现金 T+1、无价格不成交、跳空成交价 (C05/C06)."""

    def _engine(self, df, settlement=None):
        engine = CustomLoopBacktest(
            initial_cash=INITIAL_CASH,
            settlement=settlement or {"510300": "T+1"},
            cost={"slippage_base": 0.0, "impact_coefficient": 0.0,
                  "commission_rate": 0.00075, "min_commission_per_trade": 0.0,
                  "slippage_lookback_days": 20},
        )
        engine._prepare_market_data(df)
        return engine

    def test_t_plus_1_bought_today_not_sellable(self):
        """T+1 当日买入份额同日不可卖; 卖单顺延至次日."""
        df = make_ohlcv(["510300"], n_days=6)
        engine = self._engine(df)
        days = engine.dates

        assert engine._execute_buy("510300", 1000, days[1]) is not None
        assert engine.positions["510300"].available(days[1]) == 0

        trade = engine._execute_sell("510300", 1000, days[1])
        assert trade is None
        assert len(engine.pending_sells) == 1
        assert engine.pending_sells[0]["not_before"] == days[2]

        engine._open_session(days[2])
        assert engine.positions["510300"].shares == 0
        assert len([t for t in engine.trades if t.side == "SELL"]) == 1

    def test_second_lot_unlocks_independently(self):
        """两批次解锁日不同: days[2] 只能卖第一批, 余量顺延 days[3]."""
        df = make_ohlcv(["510300"], n_days=6)
        engine = self._engine(df)
        days = engine.dates

        engine._execute_buy("510300", 1000, days[1])   # 解锁 days[2]
        engine._execute_buy("510300", 200, days[2])    # 解锁 days[3]

        trade = engine._execute_sell("510300", 1200, days[2])
        assert trade is not None and trade.shares == 1000   # 只卖已解锁批次
        assert engine.positions["510300"].shares == 200
        assert engine.pending_sells == [{
            "symbol": "510300", "shares": 200, "not_before": days[3],
        }]

        engine._open_session(days[3])
        assert engine.positions["510300"].shares == 0
        assert [t.shares for t in engine.trades if t.side == "SELL"] == [1000, 200]

    def test_cash_withdrawable_lags_sell_by_one_day(self):
        """卖出款当日可用 (cash) 但 T+1 才可取 (cash_withdrawable)."""
        df = make_ohlcv(["510300"], n_days=6)
        engine = self._engine(df)
        days = engine.dates

        # days[1] 买入 1000 股 @10 → 佣金 7.5
        engine._execute_buy("510300", 1000, days[1])
        expected_after_buy = INITIAL_CASH - 10_000.0 - 7.5
        assert engine.cash == pytest.approx(expected_after_buy)
        assert engine.cash_withdrawable == pytest.approx(expected_after_buy)

        # days[2] 卖出 1000 股 @10 → 佣金 7.5; 可取仍停在买入后水平
        engine._execute_sell("510300", 1000, days[2])
        expected_after_sell = expected_after_buy + 10_000.0 - 7.5
        assert engine.cash == pytest.approx(expected_after_sell)
        assert engine.cash_withdrawable == pytest.approx(expected_after_buy)
        assert engine.pending_withdrawable == pytest.approx(10_000.0 - 7.5)

        # days[3] 开盘: 卖出款转可取
        engine._open_session(days[3])
        assert engine.cash_withdrawable == pytest.approx(expected_after_sell)

    def test_missing_open_price_no_trade(self):
        """执行日无有效开盘价 → 不成交 (停牌/数据缺失场景)."""
        df = make_ohlcv(["A", "B"], n_days=6)
        days = business_days(date(2026, 1, 5), 6)
        df = df.filter(
            ~((pl.col("symbol") == "B") & (pl.col("date") == days[2]))
        )
        engine = self._engine(df, settlement={"B": "T+1"})

        engine._rebalance_to_target({"B": 1.0}, days[2])  # 无开盘价 → 不成交
        assert engine.trades == []
        assert engine.cash == pytest.approx(INITIAL_CASH)

        engine._rebalance_to_target({"B": 1.0}, days[3])  # 有开盘价 → 成交
        assert len(engine.trades) == 1

    def test_gap_executes_at_next_open_not_assumed_stop(self):
        """跳空场景: 卖出在 T+1 开盘价成交, 不按假定的止损价成交 (C06)."""
        days = business_days(date(2026, 1, 5), 4)
        path = [(10.0, 10.0), (10.0, 10.0), (8.0, 8.0), (8.2, 8.2)]
        rows = []
        for d, (op, cl) in zip(days, path, strict=True):
            rows.append({
                "symbol": "510300", "date": d, "open": op,
                "high": max(op, cl), "low": min(op, cl), "close": cl,
                "volume": 1e6, "amount": 1e9,
            })
        df = pl.DataFrame(rows)
        engine = self._engine(df)

        engine._rebalance_to_target({"510300": 1.0}, days[1])   # 买入 @10.0
        sell_trade = engine._execute_sell("510300", 1000, days[2])
        assert sell_trade is not None
        assert sell_trade.trade_date == days[2]
        assert sell_trade.price == pytest.approx(8.0)   # 跳空开盘价 (无滑点情景)
        assert sell_trade.settlement_date == days[2]
