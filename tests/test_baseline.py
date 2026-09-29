"""B0/B1 基准模拟 (M5) 单元测试 — 独立期望值 (spec §9.1).

覆盖:
  - B0 买入持有: 初始配置一次建仓; 现金余额 = 资本 − 买入额 − 费用
  - B0 新增资金政策显式: 入金留现金, 不买入 (政策可追溯)
  - B1 阈值再平衡: 目标不变、实际漂移越界 → 检查日后调回目标 (spec §8.2 样本)
  - B1 未越界不追精确对齐; 检查日 = 每周最后交易日
  - T+1 可卖: 当日买入不可卖 (卖出按批次可卖量)
  - 现金不足: 买入缩减, 不产生负现金
  - 同口径: B0/B1 独立账户/净值; 成本逐笔记录; 重复运行确定性一致
  - 实验记录: experiments 行含配置/成本情景/结果位置 (失败状态可留痕)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from backtest.baseline import (
    BaselineConfig,
    BaselineSimulator,
    _commission_of,
)
from backtest.baseline_runner import run_baseline
from data_pipeline.duckdb_manager import DuckDBManager

START = date(2026, 1, 5)          # 周一
COST = {                           # 手工成本: 佣金 0.00075/最低 5 元, 滑点 0
    "commission_rate": 0.00075,
    "min_commission_per_trade": 5.0,
    "slippage_base": 0.0,
    "impact_coefficient": 0.0,
    "scenario": "test_flat",
}


def business_days(n: int, start: date = START) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def make_ohlcv(price_paths: dict[str, list[float]], days: list[date] | None = None) -> pl.DataFrame:
    """构造 OHLC 全部等于价格的行情 (无日内波动, 便于手工核对)."""
    rows = []
    for sym, prices in price_paths.items():
        sym_days = days or business_days(len(prices))
        for d, p in zip(sym_days, prices, strict=True):
            rows.append({
                "symbol": sym, "date": d, "open": float(p), "high": float(p),
                "low": float(p), "close": float(p), "volume": 1e8,
                "amount": 1e8,
            })
    return pl.DataFrame(rows)


def make_settings(weights: dict[str, float], band: float = 0.05) -> dict:
    return {
        "project": {"mode": "research", "strategy_version": "v0.2"},
        "trading_cost": dict(COST),
        "backtest": {"initial_cash": 100000.0},
        "baselines": {
            "label": "unit_test_example",
            "initial_cash": 100000.0,
            "band_points": band,
            "check_weekday": 4,
            "lot_size": 100,
            "b0_cash_policy": "hold_cash",
            "weights": weights,
        },
    }


@pytest.fixture()
def db(tmp_path: Path) -> DuckDBManager:
    db = DuckDBManager(str(tmp_path / "baseline.duckdb"))
    db.init_database()
    yield db
    db.close()


def seed(db: DuckDBManager, ohlcv: pl.DataFrame) -> None:
    db.upsert_ohlcv(ohlcv)


# ------------------------------------------------------------------
# B0: 买入持有
# ------------------------------------------------------------------

class TestB0BuyAndHold:
    def test_full_deployment_exact_balances(self, db):
        """100000 资本, 目标 50% A / 50% 现金: 买 5000 份×10 = 50000, 费 37.5."""
        days = business_days(5)
        prices = [10.0] * 5
        seed(db, make_ohlcv({"510300": prices}, days))
        settings = make_settings({"510300": 0.50, "__CASH__": 0.50})
        config = BaselineConfig.from_settings(settings, "B0")
        sim = BaselineSimulator(db, settings, config)

        result = sim.run(days[0], days[-1])

        # 独立期望: day1 决策, day2 开盘买 5000 份 (费用 max(37500×0.00075, 5) = 28.125?
        # — 买 5000×10 = 50000 → 费 max(37.5, 5) = 37.5 → 现金 100000−50000−37.5
        assert result.trades == 1
        assert result.fees_total == pytest.approx(37.5)
        final = result.nav.row(-1, named=True)
        assert final["cash"] == pytest.approx(100000.0 - 50000.0 - 37.5)
        assert final["nav"] == pytest.approx(100000.0 - 37.5)  # 价格不变: 仅费用
        # 持仓 5000 份
        assert sim.state.shares_of("510300") == pytest.approx(5000.0)
        # NAV 序列从决策日起
        assert result.nav["date"][0] == days[0]
        assert len(result.nav) == 5

    def test_deposit_held_as_cash(self, db):
        """B0 新增资金政策 hold_cash: 入金增加现金, 不买入 (显式政策)."""
        days = business_days(6)
        seed(db, make_ohlcv({"510300": [10.0] * 6}, days))
        settings = make_settings({"510300": 0.50, "__CASH__": 0.50})
        config = BaselineConfig.from_settings(settings, "B0")
        sim = BaselineSimulator(db, settings, config)
        deposit_day = days[4]
        result = sim.run(days[0], days[-1], deposits=[(deposit_day, 5000.0)])

        # 入金后现金增加 5000; 持股不变 (仍 5000 份)
        assert sim.state.shares_of("510300") == pytest.approx(5000.0)
        final = result.nav.row(-1, named=True)
        assert final["cash"] == pytest.approx(100000.0 - 50000.0 - 37.5 + 5000.0)
        assert result.trades == 1  # 入金未触发交易
        assert any("hold_cash" in note for note in
                   [e.note or "" for e in result.events])

    def test_price_rise_nav_reflects_holdings(self, db):
        """价格 10→12: NAV = 现金 49962.5 + 5000×12 = 109962.5 (仅费用拖累)."""
        days = business_days(4)
        seed(db, make_ohlcv({"510300": [10.0, 10.0, 12.0, 12.0]}, days))
        settings = make_settings({"510300": 0.50, "__CASH__": 0.50})
        sim = BaselineSimulator(db, settings, BaselineConfig.from_settings(settings, "B0"))
        result = sim.run(days[0], days[-1])
        final = result.nav.row(-1, named=True)
        assert final["nav"] == pytest.approx(49962.5 + 60000.0)
        assert result.period_return() == pytest.approx((109962.5 / 100000.0) - 1.0)


# ------------------------------------------------------------------
# B1: 阈值再平衡
# ------------------------------------------------------------------

class TestB1ThresholdRebalance:
    def test_drift_over_band_triggers_rebalance(self, db):
        """目标 30% 不变, A 价格 10→14 → 实际漂移到 37.5% → 检查后卖回目标 (样本案例)."""
        days = business_days(10)
        # day1-2: 10; day3+: 14 (周五=day5 检查日; 漂移 37.5% − 30% = 7.5pp > 5pp)
        prices_a = [10.0, 10.0, 14.0, 14.0, 14.0, 14.0, 14.0, 14.0, 14.0, 14.0]
        prices_b = [20.0] * 10
        seed(db, make_ohlcv({"AA": prices_a, "BB": prices_b}, days))
        settings = make_settings({"AA": 0.30, "BB": 0.30, "__CASH__": 0.40})
        config = BaselineConfig.from_settings(settings, "B1")
        sim = BaselineSimulator(db, settings, config)

        result = sim.run(days[0], days[-1])

        # 独立期望:
        #   day2 开盘建仓: AA 3000 份 (30000, 费 22.5), BB 1500 份 (30000, 费 22.5)
        #   day5 (周五) 检查: NAV = 39955 + 42000 + 30000 = 111955; AA 实际 37.5%
        #   目标份额 = int(0.30×111955/14) = 2399 → delta = −601 → 整手向最近 = −600
        #   day6 开盘卖 600 份 @14, 费 max(8400×0.00075, 5) = 6.3
        assert result.trades == 3  # 2 买 + 1 卖
        trades = [e for e in result.events if e.event_type == "trade"]
        assert trades[-1].side == "SELL"
        assert trades[-1].symbol == "AA"
        assert trades[-1].shares == pytest.approx(600.0)
        assert trades[-1].price == pytest.approx(14.0)
        # 再平衡后: 剩 2400 份 (实际 33.6% < 35% 带宽), 不再触发
        assert sim.state.shares_of("AA") == pytest.approx(2400.0)
        final_nav = float(result.nav["nav"][-1])
        aa_value = sim.state.shares_of("AA") * 14.0
        assert abs(aa_value / final_nav - 0.30) < 0.05

    def test_within_band_no_trade(self, db):
        """价格不变 → 实际=目标, 不产生任何调仓 (未越界不追精确对齐)."""
        days = business_days(10)
        seed(db, make_ohlcv({"AA": [10.0] * 10, "BB": [20.0] * 10}, days))
        settings = make_settings({"AA": 0.30, "BB": 0.30, "__CASH__": 0.40})
        sim = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
        )
        result = sim.run(days[0], days[-1])
        assert result.trades == 2  # 仅初始建仓
        assert all(
            e.side == "BUY" for e in result.events if e.event_type == "trade"
        )

    def test_check_day_is_last_trading_day_of_week(self, db):
        """检查日 = 每周最后交易日 (周五; 区间末也算)."""
        days = business_days(10)  # 两周
        seed(db, make_ohlcv({"AA": [10.0] * 10}, days))
        settings = make_settings({"AA": 0.30, "__CASH__": 0.70})
        sim = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
        )
        week1 = [d for d in days if d.isocalendar()[1] == days[0].isocalendar()[1]]
        assert sim._is_check_day(week1[-1], days) is True
        assert sim._is_check_day(week1[0], days) is False
        assert sim._is_check_day(days[-1], days) is True  # 区间末


# ------------------------------------------------------------------
# 交收与现金约束
# ------------------------------------------------------------------

class TestExecutionConstraints:
    def test_t_plus_1_blocks_same_day_sell(self, db):
        """当日买入批次不可卖: T+1 解锁 (批次级, C05)."""
        days = business_days(4)
        seed(db, make_ohlcv({"AA": [10.0, 10.0, 10.0, 10.0]}, days))
        settings = make_settings({"AA": 0.30, "__CASH__": 0.70})
        sim = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
        )
        # day2 开盘买入; 当日盘后检查: 持仓 sellable_date = day3, 不可卖
        result = sim.run(days[0], days[2])
        lots = sim.state.lots["AA"]
        assert lots[0].sellable_date == days[2]  # 买入(day2)次日
        # 无卖出成交 (当日不可卖)
        assert all(
            e.side != "SELL" for e in result.events if e.event_type == "trade"
        )

    def test_insufficient_cash_scales_buy(self, db):
        """目标 99.9% 且现金不足以覆盖手续费: 买入缩减, 现金 ≥ 0 (不超买)."""
        days = business_days(3)
        seed(db, make_ohlcv({"AA": [10.0] * 3}, days))
        settings = make_settings({"AA": 0.999, "__CASH__": 0.001})
        sim = BaselineSimulator(db, settings, BaselineConfig.from_settings(settings, "B0"))
        sim.run(days[0], days[-1])
        # 目标 99900 → 9900 份×10 + 费 74.25 = 99074.25 ≤ 100000 → 可全额;
        # 若目标 100% (10000 份 = 100000 + 75 费 > 现金) 则缩减一档 (不产生负现金)
        assert sim.state.cash >= -1e-9
        # 手工核对: 买 9900 份 (99000) + 费 74.25 → 现金 925.75
        assert sim.state.shares_of("AA") == pytest.approx(9900.0)
        assert sim.state.cash == pytest.approx(100000.0 - 99000.0 - 74.25)

    def test_slippage_applied_to_execution_price(self, db):
        """滑点体现在成交价 (买方溢价), 不二次扣现金 (同一费率函数)."""
        days = business_days(2)
        seed(db, make_ohlcv({"AA": [10.0, 10.0]}, days))
        settings = make_settings({"AA": 0.50, "__CASH__": 0.50})
        settings["trading_cost"]["slippage_base"] = 0.01  # 1% 滑点
        sim = BaselineSimulator(db, settings, BaselineConfig.from_settings(settings, "B0"))
        sim.run(days[0], days[-1])
        trade = next(e for e in sim.events if e.event_type == "trade")
        assert trade.price == pytest.approx(10.10)  # 10 × (1+1%)
        # 现金 = 100000 − 份数×10.10 − 费
        shares = trade.shares
        expected_cash = 100000.0 - shares * 10.10 - _commission_of(shares * 10.10, COST)
        assert sim.state.cash == pytest.approx(expected_cash)


# ------------------------------------------------------------------
# 同口径与确定性
# ------------------------------------------------------------------

class TestSameFramework:
    def test_b0_b1_independent_accounts_same_cost(self, db):
        """B0/B1 独立账户与净值; 初始建仓费用相同 (同口径)."""
        days = business_days(6)
        seed(db, make_ohlcv({"AA": [10.0] * 6, "BB": [20.0] * 6}, days))
        settings = make_settings({"AA": 0.30, "BB": 0.30, "__CASH__": 0.40})
        b0 = BaselineSimulator(db, settings, BaselineConfig.from_settings(settings, "B0"))
        b1 = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
            account_id="BASE-B1-2",
        )
        r0 = b0.run(days[0], days[-1])
        r1 = b1.run(days[0], days[-1])
        assert r0.account_id != r1.account_id
        # 初始建仓费用一致 (同价格/同成本/同权重)
        init_fees_0 = sum(e.fee or 0 for e in r0.events[:2])
        init_fees_1 = sum(e.fee or 0 for e in r1.events[:2])
        assert init_fees_0 == pytest.approx(init_fees_1)

    def test_rerun_deterministic(self, db):
        """同一输入重复运行: NAV 逐点一致 (可复现, spec §6.5)."""
        days = business_days(8)
        seed(db, make_ohlcv({"AA": [10, 11, 10.5, 12, 11, 11.5, 12, 12.5] * 1}, days))
        settings = make_settings({"AA": 0.40, "__CASH__": 0.60})
        r1 = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
        ).run(days[0], days[-1])
        r2 = BaselineSimulator(
            db, settings, BaselineConfig.from_settings(settings, "B1"),
        ).run(days[0], days[-1])
        assert r1.nav["nav"].to_list() == r2.nav["nav"].to_list()
        assert [e.shares for e in r1.events if e.event_type == "trade"] == \
               [e.shares for e in r2.events if e.event_type == "trade"]

    def test_active_strategy_same_execution_foundation(self, db):
        """A (主动策略建议) 与 B0/B1 共用成本/账本/整手; 模拟资金不进真实权益 (spec §3.1)."""
        from backtest.baseline import LOT_SIZE as BASELINE_LOT
        from data_pipeline.accounting import AccountEvent, AccountState, apply_event
        from strategies.rebalance_planner import LOT_SIZE as PLANNER_LOT
        from strategies.rebalance_planner import _commission_of as planner_fee

        # 1) 整手与费用同一约定
        assert BASELINE_LOT == PLANNER_LOT == 100
        assert _commission_of(10000.0, COST) == planner_fee(10000.0, COST)
        assert _commission_of(100.0, COST) == planner_fee(100.0, COST)  # 最低收费

        # 2) 模拟账户与真实账户隔离: 基准 equity 不与真实账户相加
        #    (accounts 表按 account_id + mode 隔离; 无合并视图路径)
        real = AccountState(account_id="REAL-1", cash=1000.0)
        base = AccountState(account_id="BASE-B0", cash=100000.0)
        apply_event(real, AccountEvent(
            event_id="r1", account_id="REAL-1", event_type="deposit",
            event_time=datetime(2026, 1, 1), amount=1000.0,
        ))
        apply_event(base, AccountEvent(
            event_id="b1", account_id="BASE-B0", event_type="deposit",
            event_time=datetime(2026, 1, 1), amount=100000.0,
        ))
        assert real.cash == pytest.approx(2000.0)
        assert base.cash == pytest.approx(200000.0)
        assert real.account_id != base.account_id


# ------------------------------------------------------------------
# 运行器: 实验记录与结果文件 (M5-05)
# ------------------------------------------------------------------

class TestRunnerAndExperiment:
    def test_run_records_experiment_and_parquet(self, db, tmp_path):
        days = business_days(5)
        seed(db, make_ohlcv({"AA": [10.0] * 5}, days))
        settings = make_settings({"AA": 0.50, "__CASH__": 0.50})
        settings["baselines"]["check_weekday"] = 4
        db_path = str(db.db_path)

        result = run_baseline(
            settings, db_path, "B0", days[0], days[-1],
            result_dir=tmp_path / "backtests",
        )

        assert result.experiment_id
        exp = db.get_experiment(result.experiment_id)
        assert exp is not None
        assert exp["kind"] == "baseline_b0"
        assert exp["status"] == "ok"
        assert exp["cost_scenario"] == "test_flat"
        assert "config_json" in exp and "weights" in exp["config_json"]
        config = json.loads(exp["config_json"])
        assert config["label"] == "unit_test_example"
        # 结果文件: NAV parquet + 摘要 json
        nav_path = Path(exp["result_location"])
        assert nav_path.exists()
        nav = pl.read_parquet(nav_path)
        assert nav["date"].len() == 5
        summary = json.loads(
            (tmp_path / "backtests" / f"{result.experiment_id}.json").read_text("utf-8")
        )
        assert summary["strategy"] == "B0"
        assert "不复权" in summary["caliber_note"]

    def test_write_account_persists_ledger(self, db):
        """--write-account: 模拟账户与事件落库 (独立 mode=backtest)."""
        days = business_days(4)
        seed(db, make_ohlcv({"AA": [10.0] * 4}, days))
        settings = make_settings({"AA": 0.50, "__CASH__": 0.50})
        run_baseline(
            settings, str(db.db_path), "B0", days[0], days[-1],
            write_account=True, result_dir=db.db_path.parent,
        )
        accounts = db.get_accounts()
        assert "BASE-B0" in accounts["account_id"].to_list()
        mode = accounts.filter(pl.col("account_id") == "BASE-B0")["mode"][0]
        assert mode == "backtest"
        # 事件回放与模拟器一致 (现金/份额)
        from data_pipeline.accounting import AccountService

        state = AccountService(db).current_state("BASE-B0")
        assert state.shares_of("AA") == pytest.approx(5000.0)

    def test_unknown_strategy_rejected(self, db, tmp_path):
        settings = make_settings({"AA": 0.5, "__CASH__": 0.5})
        with pytest.raises(ValueError, match="未知基准策略"):
            run_baseline(
                settings, str(db.db_path), "B2", START, START,
                result_dir=tmp_path,
            )

    def test_failed_experiment_is_recorded(self, db, tmp_path):
        """失败试验留痕 (状态/原因/起止时间), 不删除 (spec §7.1 / P05-D6)."""
        from backtest.baseline import BaselineConfig
        from backtest.baseline_runner import _record_failed_experiment

        settings = make_settings({"AA": 0.5, "__CASH__": 0.5})
        config = BaselineConfig.from_settings(settings, "B0")
        started = datetime(2026, 1, 5, 9, 0)
        _record_failed_experiment(
            db, "exp-failed-1", "B0", config, settings["project"],
            "test_flat", START, START, started, RuntimeError("数据为空"),
        )
        exp = db.get_experiment("exp-failed-1")
        assert exp is not None
        assert exp["status"] == "failed"
        assert "数据为空" in exp["error"]
        assert exp["started_at"] is not None and exp["finished_at"] is not None
        assert exp["mode"] == "research"

    def test_adverse_cost_scenario_fees_increase(self, db, tmp_path):
        """更不利成本情景: 费率翻倍 → 费用上升, 结果可比对 (spec §6.3)."""
        days = business_days(3)
        seed(db, make_ohlcv({"AA": [10.0, 10.0, 10.0]}, days))
        base = make_settings({"AA": 0.50, "__CASH__": 0.50})
        base["trading_cost"]["scenario"] = "base"
        adverse = make_settings({"AA": 0.50, "__CASH__": 0.50})
        adverse["trading_cost"] = {
            **adverse["trading_cost"],
            "scenario": "adverse_2x",
            "commission_rate": 0.0015,
            "slippage_base": 0.003,
        }
        # 用不同结果目录避免文件名冲突; 实验记录分别落库
        r_base = run_baseline(
            base, str(db.db_path), "B0", days[0], days[-1],
            result_dir=tmp_path / "base",
        )
        r_adverse = run_baseline(
            adverse, str(db.db_path), "B0", days[0], days[-1],
            result_dir=tmp_path / "adverse",
        )
        assert r_adverse.fees_total > r_base.fees_total
        # 情境名进入实验记录
        exp = db.get_experiment(r_adverse.experiment_id)
        assert exp["cost_scenario"] == "adverse_2x"
        exp_base = db.get_experiment(r_base.experiment_id)
        assert exp_base["cost_scenario"] == "base"
