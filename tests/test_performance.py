"""基础绩效核算 (M6) 单元测试 — 独立期望值 (spec §7.4 / §8.2 样本).

覆盖:
  - 金额核对闭合: 期末 − 调整起点 − 净入金 = 投资盈亏 = 持仓贡献 + 分红 − 费用
  - 入金不增加收益 (零行情收益时入金 → 收益 0, spec §8.2 样本)
  - 费用/分红分项单列; 起点日事件不重复计
  - 提出金不产生收益
  - 日频 TWR 与简单期间收益一致 (无期中现金流)
  - 多个模拟账户同口径对照 (独立净值, 不合并)
  - 与 M5 基准模拟联动: B0 的账本事件 → 绩效报告与模拟器手工期望一致
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from backtest.performance import (
    compare_accounts,
    compute_performance,
    load_nav_series,
    performance_for_account,
)
from data_pipeline.accounting import AccountEvent
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.market_data import PriceSeries

D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)
D3 = date(2026, 1, 7)


def prices_flat_rise() -> PriceSeries:
    """X: 10 → 12 → 12 (10→12 = +20%)."""
    return PriceSeries(pl.DataFrame([
        {"symbol": "X", "date": D1, "open": 10.0, "high": 10.0, "low": 10.0,
         "close": 10.0, "volume": 1e8, "amount": 1e8},
        {"symbol": "X", "date": D2, "open": 10.0, "high": 10.0, "low": 10.0,
         "close": 12.0, "volume": 1e8, "amount": 1e8},
        {"symbol": "X", "date": D3, "open": 12.0, "high": 12.0, "low": 12.0,
         "close": 12.0, "volume": 1e8, "amount": 1e8},
    ]))


def ev(
    eid: str, kind: str, day: date, hour: int = 15, **kwargs,
) -> AccountEvent:
    return AccountEvent(
        event_id=eid, account_id="ACC-1", event_type=kind,
        event_time=datetime(day.year, day.month, day.day, hour), **kwargs,
    )


def initial(amount: float = 100000.0) -> AccountEvent:
    return ev("init", "initial", D1, amount=amount)


def buy_x(shares: float = 5000.0, price: float = 10.0, fee: float = 37.5) -> AccountEvent:
    return ev("trade-1", "trade", D1, symbol="X", side="BUY", shares=shares,
              price=price, fee=fee)


# ------------------------------------------------------------------
# 金额核对 (P05-D7)
# ------------------------------------------------------------------

class TestPnLReconciliation:
    def test_identity_holds_with_fee_and_price_rise(self):
        """费用 37.5 + 持股 5000 份 10→12: 投资盈亏 9962.5 = 10000 − 37.5."""
        report = compute_performance(
            [initial(), buy_x()], prices=prices_flat_rise(), end=D3,
        )
        assert report.investment_pnl == pytest.approx(9962.5)
        # 分项: 持仓贡献 10000, 费用 37.5, 分红 0
        assert report.reconciled_pnl == pytest.approx(10000.0)
        assert report.fees == pytest.approx(37.5)
        assert report.dividends == pytest.approx(0.0)
        # 核对闭合 (未解释差额为 0)
        assert report.unexplained == pytest.approx(0.0)
        # 期末权益 = 现金 49962.5 + 60000 = 109962.5
        assert report.nav_end == pytest.approx(109962.5)

    def test_fee_only_no_price_change(self):
        """价格不变: 投资盈亏 = −费用 (−37.5), 持仓贡献 0."""
        flat = PriceSeries(pl.DataFrame([
            {"symbol": "X", "date": d, "open": 10.0, "high": 10.0, "low": 10.0,
             "close": 10.0, "volume": 1e8, "amount": 1e8}
            for d in (D1, D2, D3)
        ]))
        report = compute_performance([initial(), buy_x()], prices=flat, end=D3)
        assert report.investment_pnl == pytest.approx(-37.5)
        assert report.reconciled_pnl == pytest.approx(0.0)
        assert report.unexplained == pytest.approx(0.0)

    def test_dividend_income_separate_from_deposit(self):
        """分红是投资收益: accrual 500 → 盈亏 +500; 入金另计不混同."""
        events = [
            initial(),
            ev("acc-1", "dividend_accrual", D2, amount=500.0),
            ev("dep-1", "deposit", D2, amount=10000.0),
        ]
        report = compute_performance(events, prices=prices_flat_rise(), end=D3)
        assert report.dividends == pytest.approx(500.0)
        assert report.net_external_flow == pytest.approx(10000.0)  # 入金单列
        assert report.investment_pnl == pytest.approx(500.0)
        assert report.unexplained == pytest.approx(0.0)

    def test_withdrawal_does_not_create_pnl(self):
        """出金不产生收益: 期末 − 期初 − (−出金) = 0."""
        events = [
            initial(),
            ev("wd-1", "withdrawal", D2, amount=3000.0),
        ]
        report = compute_performance(events, prices=prices_flat_rise(), end=D3)
        assert report.net_external_flow == pytest.approx(-3000.0)
        assert report.investment_pnl == pytest.approx(0.0)
        assert report.unexplained == pytest.approx(0.0)


# ------------------------------------------------------------------
# 收益口径 (spec §7.4 / §8.2 样本)
# ------------------------------------------------------------------

class TestReturnCaliber:
    def test_zero_price_return_deposit_does_not_raise_return(self):
        """零行情收益 + 入金: 资产增加、投资收益与收益率不变 (样本案例)."""
        flat = PriceSeries(pl.DataFrame([
            {"symbol": "X", "date": d, "open": 10.0, "high": 10.0, "low": 10.0,
             "close": 10.0, "volume": 1e8, "amount": 1e8}
            for d in (D1, D2, D3)
        ]))
        events = [
            initial(),
            ev("dep-1", "deposit", D3, amount=20000.0),
        ]
        report = compute_performance(events, prices=flat, end=D3)
        assert report.investment_pnl == pytest.approx(0.0)
        assert report.simple_return() == pytest.approx(0.0)
        assert report.twr() == pytest.approx(0.0)
        # 资产端确实增加 (入金进入现金): 期末 = 120000
        assert report.nav_end == pytest.approx(120000.0)

    def test_price_rise_return_caliber(self):
        """价格 10→12 全仓: 简单收益与 TWR 一致 (无期中现金流)."""
        events = [initial(), buy_x()]
        report = compute_performance(events, prices=prices_flat_rise(), end=D3)
        expected = (109962.5 - 100000.0) / 100000.0
        assert report.simple_return() == pytest.approx(expected)
        assert report.twr() == pytest.approx(expected)

    def test_mid_period_deposit_not_counted_as_return(self):
        """期中入金: 收益分母不含入金 (现金流调整)."""
        events = [
            initial(),
            buy_x(),
            ev("dep-1", "deposit", D2, amount=10000.0),
        ]
        report = compute_performance(events, prices=prices_flat_rise(), end=D3)
        # 期末 119962.5; 净入金 10000 → (119962.5 − 10000) / 100000 − 1 = 9.9625%
        assert report.simple_return() == pytest.approx(0.099625)
        assert report.investment_pnl == pytest.approx(9962.5)

    def test_cashflow_timing_note_present(self):
        """日线数据现金流时点约定必须随报告 (近似标注, spec §7.4)."""
        report = compute_performance(
            [initial(), buy_x()], prices=prices_flat_rise(), end=D3,
        )
        assert any("日终估值点" in note for note in report.notes)
        assert "近似" in report.summary()


# ------------------------------------------------------------------
# 缺口 / 估值
# ------------------------------------------------------------------

class TestValuationGaps:
    def test_missing_price_flagged(self):
        """持仓缺价: 计入估值缺口, 提示金额核对不可用."""
        events = [initial(), buy_x()]
        report = compute_performance(events, prices=None, end=D3)
        assert any("估值缺口" in note for note in report.notes)

    def test_no_events_rejected(self):
        with pytest.raises(ValueError):
            compute_performance([], prices=prices_flat_rise())


# ------------------------------------------------------------------
# 账户路径与多账户对照
# ------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> DuckDBManager:
    db = DuckDBManager(str(tmp_path / "perf.duckdb"))
    db.init_database()
    yield db
    db.close()


class TestAccountAndComparison:
    def _seed(self, db: DuckDBManager, account_id: str, mode: str = "backtest") -> None:
        from data_pipeline.accounting import AccountService

        svc = AccountService(db)
        svc.ensure_account(account_id, f"测试 {account_id}", mode=mode)
        svc.import_events([
            AccountEvent(event_id=f"{account_id}-i", account_id=account_id,
                         event_type="initial", event_time=datetime(2026, 1, 5),
                         amount=100000.0),
            AccountEvent(event_id=f"{account_id}-t", account_id=account_id,
                         event_type="trade", event_time=datetime(2026, 1, 5, 15),
                         symbol="X", side="BUY", shares=5000.0, price=10.0,
                         fee=37.5),
        ])

    def test_performance_for_account_reads_mode(self, db):
        self._seed(db, "BASE-A")
        db.upsert_ohlcv(prices_flat_rise_df())
        report = performance_for_account(db, "BASE-A", end=D3)
        assert report.mode == "backtest"
        assert report.investment_pnl == pytest.approx(9962.5)

    def test_compare_accounts_independent_series(self, db):
        """多账户对照: 归一化净值独立, 不与真实账户合并 (spec §3.1)."""
        self._seed(db, "BASE-A")
        self._seed(db, "BASE-B")
        db.upsert_ohlcv(prices_flat_rise_df())
        summaries, wide = compare_accounts(db, ["BASE-A", "BASE-B", "MISSING"])
        assert {s.account_id for s in summaries} == {"BASE-A", "BASE-B"}
        assert "BASE-A" in wide.columns and "BASE-B" in wide.columns
        # 首日归一 = 100
        first = wide.row(0, named=True)
        assert first["BASE-A"] == pytest.approx(100.0)

    def test_nav_series_daily(self, db):
        self._seed(db, "BASE-A")
        db.upsert_ohlcv(prices_flat_rise_df())
        nav = load_nav_series(db, "BASE-A")
        assert nav["date"].to_list() == [D1, D2, D3]
        # 逐日: 99962.5 (费后) → 109962.5 → 109962.5
        values = nav["nav"].to_list()
        assert values[0] == pytest.approx(99962.5)
        assert values[1] == pytest.approx(109962.5)


def prices_flat_rise_df() -> pl.DataFrame:
    return pl.DataFrame([
        {"symbol": "X", "date": D1, "open": 10.0, "high": 10.0, "low": 10.0,
         "close": 10.0, "volume": 1e8, "amount": 1e8},
        {"symbol": "X", "date": D2, "open": 10.0, "high": 10.0, "low": 10.0,
         "close": 12.0, "volume": 1e8, "amount": 1e8},
        {"symbol": "X", "date": D3, "open": 12.0, "high": 12.0, "low": 12.0,
         "close": 12.0, "volume": 1e8, "amount": 1e8},
    ])


# ------------------------------------------------------------------
# 与 M5 基准联动 (真实路径)
# ------------------------------------------------------------------

class TestBaselineIntegration:
    def test_baseline_b0_performance_matches_simulator(self, db):
        """B0 模拟账本 → 绩效报告与模拟器手工期望一致 (同口径闭环)."""
        from backtest.baseline import BaselineConfig, BaselineSimulator

        db.upsert_ohlcv(prices_flat_rise_df())
        settings = {
            "project": {"mode": "research", "strategy_version": "v0.2"},
            "trading_cost": {
                "commission_rate": 0.00075, "min_commission_per_trade": 5.0,
                "slippage_base": 0.0, "impact_coefficient": 0.0,
                "scenario": "test_flat",
            },
            "baselines": {
                "label": "perf_test", "initial_cash": 100000.0,
                "band_points": 0.05, "check_weekday": 4, "lot_size": 100,
                "b0_cash_policy": "hold_cash",
                "weights": {"X": 0.50, "__CASH__": 0.50},
            },
        }
        sim = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B0"),
        )
        result = sim.run(D1, D3)
        report = compute_performance(
            result.events, prices=PriceSeries.from_db(db), end=D3,
        )
        # 模拟器: 买 5000 份 ×10 + 费 37.5; 期末 109962.5
        assert report.investment_pnl == pytest.approx(9962.5)
        assert report.fees == pytest.approx(37.5)
        assert report.unexplained == pytest.approx(0.0)
        assert report.twr() == pytest.approx(0.099625)
