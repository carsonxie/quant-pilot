"""持有期状态化止损与冷却 (M4-06, C06) 单元测试 — 独立期望值.

覆盖:
  - 高水位只在持有期维护 (entry_date 起), 不是固定回看窗口
  - 止损线棘轮 (只上移); 触发判定用 T 日收盘
  - 触发 → pending_exit 保留至卖出; 卖出后进入冷却
  - 冷却期内不出买单 (cooldown_symbols 供规划器消费)
  - 重新建仓重置高水位与止损状态 (spec §5.5)
  - 停牌/无行情持续展示风险
  - ATR (Wilder) 手工核对 (见各用例注释)
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from data_pipeline.duckdb_manager import DuckDBManager
from strategies.risk_manager import RiskManager
from strategies.strategy_states import (
    PositionState,
    cooldown_symbols,
    load_position_states,
    persist_position_states,
    stop_distance_display,
    update_position_states,
)

RM = RiskManager(atr_period=14, atr_multiplier=2.0)

# 手工基线: 前 15 行 [85,80,70,60,75,90,100,78,100×7]
#   TR = [0,5,10,10,15,15,10,22,22,0,0,0,0,0,0]
#   ATR₀ = mean(TR[1:15]) = (5+10+10+15+15+10+22+22)/14 = 109/14 = 7.785714286
# 追加 78: TR=22 → ATR = (7.785714286×13 + 22)/14 = 8.801020408
# 追加 100: TR=0 → ATR = 7.785714286×13/14 = 7.229591837
BASE = [85, 80, 70, 60, 75, 90, 100, 78] + [100] * 7
DAY15 = date(2026, 8, 15)
DAY16 = date(2026, 8, 16)


def ohlcv_from_prices(
    symbol: str, prices: list[float], start: date = date(2026, 8, 1),
) -> pl.DataFrame:
    """构造 OHLC=close=price 的行情 (日期连续自然日, 简化)."""
    dates = [start + timedelta(days=i) for i in range(len(prices))]
    return pl.DataFrame({
        "symbol": [symbol] * len(prices),
        "date": dates,
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [1e8] * len(prices), "amount": [1e8] * len(prices),
    })


# ------------------------------------------------------------------
# ATR 序列 (手工期望值)
# ------------------------------------------------------------------

class TestAtrSeries:
    def test_wilder_atr_manual_value(self):
        """TR[1:15] 均值 = 109/14 = 7.785714286 (独立期望)."""
        df = ohlcv_from_prices("T", BASE)
        series = RM.calc_atr_series(df)
        assert series is not None
        assert series[-1] == pytest.approx(7.785714285714286, abs=1e-9)

    def test_wilder_atr_one_extension(self):
        """追加 78: TR=22 → ATR = (7.785714286×13 + 22)/14 = 8.801020408... (Wilder)."""
        df = ohlcv_from_prices("T", BASE + [78])
        series = RM.calc_atr_series(df)
        assert series is not None
        assert series[-1] == pytest.approx(8.801020408163264, abs=1e-9)

    def test_atr_series_insufficient_data(self):
        df = ohlcv_from_prices("T", [10.0] * 14)
        assert RM.calc_atr_series(df) is None


# ------------------------------------------------------------------
# 状态更新: 高水位 / 棘轮 / 触发
# ------------------------------------------------------------------

class TestPositionStateUpdate:
    def test_high_water_and_stop_on_holding_period(self):
        """持有期高水位 = 100; stop = 100 − 2×7.785714286 = 84.428571 (独立期望)."""
        ohlcv = ohlcv_from_prices("T", BASE)
        states, triggered, cooldown = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY15, cooldown_days=5,
        )
        st = states["T"]
        assert st.high_water == pytest.approx(100.0)
        assert st.atr == pytest.approx(7.785714285714286)
        assert st.stop_price == pytest.approx(84.42857142857143)
        assert triggered == [] and st.pending_exit is False
        assert cooldown == {}

    def test_stop_trigger_on_close_at_threshold(self):
        """day16 收 78 ≤ stop 82.397959 → 触发; pending_exit 保留."""
        ohlcv = ohlcv_from_prices("T", BASE + [78])
        states, triggered, _cd = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16, cooldown_days=5,
        )
        st = states["T"]
        # stop = 100 − 2×8.801020408 = 82.39795918367346 (独立期望)
        assert st.stop_price == pytest.approx(82.39795918367346, abs=1e-6)
        assert triggered == ["T"]
        assert st.pending_exit is True and st.stop_triggered is True
        assert st.exit_date == DAY16
        assert st.cooldown_until == DAY16 + timedelta(days=5)
        assert "止损触发" in (st.exit_reason or "")

    def test_stop_ratchet_never_moves_down(self):
        """棘轮: 止损线只上移; 价格回升后基准线下降但旧止损保留."""
        ohlcv16 = ohlcv_from_prices("T", BASE + [100])
        states, _t, _c = update_position_states(
            {}, RM, ohlcv16, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16, cooldown_days=5,
        )
        stop_before = states["T"].stop_price
        # ATR₀ = 7.2; +100 行 TR=0 → ATR = 7.2×13/14 = 6.685714286
        # stop = 100 − 2×6.685714286 = 86.628571429 (独立期望)
        assert stop_before == pytest.approx(85.54081632653062, abs=1e-6)

        # 追加 78 → TR=22 → ATR = 8.284620991, base stop = 83.430758 < 86.628571
        ohlcv17 = ohlcv_from_prices("T", BASE + [100, 78])
        states2, triggered2, _c2 = update_position_states(
            states, RM, ohlcv17, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16 + timedelta(days=1), cooldown_days=5,
        )
        assert states2["T"].stop_price == pytest.approx(stop_before)  # 不下移
        assert triggered2 == ["T"]  # 78 ≤ 85.540816 触发

    def test_high_water_only_holding_period(self):
        """持有期从 day8 起: 高水位取持有期内最高 (100), 早期 100 不改变结论."""
        ohlcv = ohlcv_from_prices("T", BASE)
        # 持有期 = day15 单日 → 高水位 = 100 (day15 close)
        states, _t, _c = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": DAY15},
            as_of=DAY15, cooldown_days=5,
        )
        assert states["T"].high_water == pytest.approx(100.0)

    def test_reentry_resets_state(self):
        """重新建仓 (新 entry_date) → 旧高水位/止损重置后按新持有期重算."""
        ohlcv = ohlcv_from_prices("T", BASE + [78])
        old = PositionState(
            symbol="T", has_position=True, entry_date=date(2026, 7, 1),
            high_water=200.0, stop_price=180.0,
        )
        states, _t, _c = update_position_states(
            {"T": old}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": DAY16},  # 新持有期 (回到旧价 78)
            as_of=DAY16, cooldown_days=5,
        )
        st = states["T"]
        # 重置后高水位 = 持有期(day16)内最高 = 78; ATR = 8.801020408
        # stop = 78 − 2×8.801020408 = 60.39795918367347 (独立期望, 无旧 180 影响)
        assert st.high_water == pytest.approx(78.0)
        assert st.stop_price == pytest.approx(60.39795918367347, abs=1e-6)
        assert st.pending_exit is False  # 新持有期状态重置

    def test_pending_exit_kept_until_sold(self):
        """触发后未能卖出 (仍持仓): 每轮持续提示, 状态保留."""
        ohlcv = ohlcv_from_prices("T", BASE + [78])
        states, triggered, _c = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16, cooldown_days=5,
        )
        assert triggered == ["T"]
        # 下一日仍持仓: 继续提示 (价格继续低于止损)
        ohlcv2 = ohlcv_from_prices("T", BASE + [78, 77])
        states2, triggered2, _ = update_position_states(
            states, RM, ohlcv2, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16 + timedelta(days=1), cooldown_days=5,
        )
        assert triggered2 == ["T"]
        assert states2["T"].pending_exit is True

    def test_sold_position_enters_cooldown(self):
        """退出完成 (份额归零) → 进入冷却; 冷却期内禁止新买; 到期清理."""
        ohlcv = ohlcv_from_prices("T", BASE + [78])
        states, triggered, _c = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY16, cooldown_days=5,
        )
        assert triggered == ["T"]
        # 已卖出: held_symbols 不再包含 T
        states_out, _t2, cooldown = update_position_states(
            states, RM, ohlcv, held_symbols=set(),
            entry_dates={}, as_of=DAY16, cooldown_days=5,
        )
        st = states_out["T"]
        assert st.has_position is False and st.pending_exit is False
        assert st.cooldown_until == DAY16 + timedelta(days=5)
        assert "T" in cooldown and "冷却" in cooldown["T"]

        # 冷却到期 → 记录清理, 可重新进入
        states_after, _t3, cooldown_after = update_position_states(
            states_out, RM, ohlcv, held_symbols=set(),
            entry_dates={}, as_of=DAY16 + timedelta(days=6), cooldown_days=5,
        )
        assert "T" not in states_after
        assert cooldown_after == {}

    def test_no_price_keeps_state(self):
        """标的无行情 → 不误触发, 无状态行."""
        ohlcv = ohlcv_from_prices("OTHER", [10.0] * 20)
        states, triggered, _c = update_position_states(
            {}, RM, ohlcv, held_symbols={"T"},
            entry_dates={"T": date(2026, 8, 1)},
            as_of=DAY15, cooldown_days=5,
        )
        assert triggered == []
        assert "T" not in states


# ------------------------------------------------------------------
# 持久化与展示
# ------------------------------------------------------------------

class TestPersistence:
    @pytest.fixture()
    def db(self, tmp_path):
        db = DuckDBManager(str(tmp_path / "states.duckdb"))
        db.init_database()
        yield db
        db.close()

    def test_roundtrip(self, db):
        state = PositionState(
            symbol="510300", has_position=True, entry_date=date(2026, 8, 1),
            high_water=12.5, atr=0.3, stop_price=11.9,
        )
        n = persist_position_states(db, {"510300": state})
        assert n == 1
        loaded = load_position_states(db)
        assert loaded["510300"].high_water == pytest.approx(12.5)
        assert loaded["510300"].stop_price == pytest.approx(11.9)
        assert loaded["510300"].has_position is True

    def test_cooldown_display(self):
        state = PositionState(
            symbol="T", has_position=False, exit_date=date(2026, 8, 16),
            exit_reason="ATR止损触发", cooldown_until=date(2026, 8, 21),
        )
        cooldown = cooldown_symbols({"T": state}, date(2026, 8, 18))
        assert "T" in cooldown and "冷却" in cooldown["T"]
        assert cooldown_symbols({"T": state}, date(2026, 8, 22)) == {}

    def test_stop_distance_display_flags_pending(self):
        ohlcv = ohlcv_from_prices("T", BASE + [78])
        states = {
            "T": PositionState(
                symbol="T", has_position=True, high_water=100.0,
                stop_price=86.628571, pending_exit=True,
            ),
        }
        lines = stop_distance_display(states, ohlcv, {"T"}, DAY16)
        assert len(lines) == 1
        assert "已触发待执行" in lines[0]
