"""风控模块单元测试.

验证: ATR 计算 (Wilder 平滑), 移动止损, 止损线棘轮约束, 宏观骤降减仓.
"""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from strategies.risk_manager import (
    RiskManager,
    RiskReport,
    StopLevel,
    from_settings,
)


# ------------------------------------------------------------------
# 辅助: 生成合成 OHLCV
# ------------------------------------------------------------------

def make_ohlcv(
    symbols: list[str],
    n_days: int = 60,
    base_price: float = 3.0,
    daily_vol: float = 0.012,
    trend: float = 0.0,
    seed: int = 42,
) -> pl.DataFrame:
    """生成多标的合成 OHLCV.

    Args:
        symbols: 标的列表
        n_days: 交易日数
        base_price: 起始价
        daily_vol: 日波动率
        trend: 日漂移 (正=上涨, 负=下跌)
    """
    rng = np.random.default_rng(seed)
    dfs = []

    for i, sym in enumerate(symbols):
        eff_seed = seed + i * 1000
        sym_rng = np.random.default_rng(eff_seed)
        returns = sym_rng.normal(trend, daily_vol, n_days)
        prices = base_price * np.exp(np.cumsum(returns))

        # OHLC: 在 close 基础上模拟日内波动
        intraday_noise = 0.005
        highs = prices * (1 + np.abs(sym_rng.normal(0, intraday_noise, n_days)))
        lows = prices * (1 - np.abs(sym_rng.normal(0, intraday_noise, n_days)))
        opens = prices * (1 + sym_rng.normal(0, intraday_noise * 0.5, n_days))

        dates = []
        d = date(2025, 1, 2)
        while len(dates) < n_days:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)

        dfs.append(pl.DataFrame({
            "symbol": [sym] * n_days,
            "date": dates[:n_days],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": prices,
            "volume": [1e8] * n_days,
            "amount": [p * 1e8 for p in prices],
        }))

    return pl.concat(dfs)


def make_trending_ohlcv(
    symbol: str = "TEST",
    n_days: int = 40,
    start_price: float = 3.0,
    change_per_day: float = 0.005,
) -> pl.DataFrame:
    """生成确定性趋势 OHLCV, 方便精确验证 ATR 和止损计算."""
    prices = [start_price * (1 + change_per_day) ** i for i in range(n_days)]
    dates = []
    d = date(2025, 1, 2)
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)

    return pl.DataFrame({
        "symbol": [symbol] * n_days,
        "date": dates[:n_days],
        "open": [p * 0.99 for p in prices],
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": [1e8] * n_days,
        "amount": [p * 1e8 for p in prices],
    })


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def rm():
    return RiskManager(atr_period=14, atr_multiplier=2.0)


@pytest.fixture
def multi_etf_df():
    """3 ETFs, 60 天历史."""
    return make_ohlcv(["ETF_A", "ETF_B", "ETF_C"], n_days=60, seed=7)


@pytest.fixture
def long_trend_df():
    """长趋势数据 (足够 ATR 计算)."""
    return make_trending_ohlcv(n_days=40)


# ------------------------------------------------------------------
# ATR 计算
# ------------------------------------------------------------------

class TestATR:
    def test_atr_positive(self, rm, multi_etf_df):
        """ATR 应为正数."""
        sym_df = multi_etf_df.filter(pl.col("symbol") == "ETF_A")
        atr = rm._calc_atr(sym_df)
        assert atr is not None
        assert atr > 0

    def test_atr_on_stable_prices(self, rm):
        """价格稳定 → ATR 小."""
        n = 40
        prices = np.linspace(3.0, 3.05, n)  # 几乎直线
        df = pl.DataFrame({
            "symbol": ["S"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": prices * 1.005,
            "low": prices * 0.995,
            "close": prices,
        })
        atr = rm._calc_atr(df)
        assert atr is not None
        assert atr < 0.05, f"Stable prices should have low ATR, got {atr:.4f}"

    def test_atr_on_volatile_prices(self, rm):
        """价格剧烈波动 → ATR 大."""
        n = 40
        rng = np.random.default_rng(99)
        prices = [3.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + rng.normal(0, 0.04)))
        df = pl.DataFrame({
            "symbol": ["V"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": [p * (1 + abs(rng.normal(0, 0.02))) for p in prices],
            "low": [p * (1 - abs(rng.normal(0, 0.02))) for p in prices],
            "close": prices,
        })
        atr = rm._calc_atr(df)
        assert atr is not None
        assert atr > 0.08, f"Volatile prices should have high ATR, got {atr:.4f}"

    def test_atr_insufficient_data(self, rm):
        """不足 ATR 周期 → None."""
        df = make_ohlcv(["X"], n_days=10, seed=1)
        sym_df = df.filter(pl.col("symbol") == "X")
        atr = rm._calc_atr(sym_df)
        assert atr is None

    def test_atr_wilder_smoothing(self, rm):
        """Wilder 平滑: 最终 ATR 反映近期波动."""
        # 用确定性数据验证 ATR 合理
        df = make_trending_ohlcv(n_days=50)
        atr = rm._calc_atr(df)
        assert atr is not None
        # 上涨 0.5%/天, high-low ≈ 4% of price, ATR 应在合理范围
        assert 0.01 < atr < 0.20, f"ATR={atr:.4f} out of expected range"

    def test_atr_custom_period(self):
        """自定义 ATR 周期."""
        rm10 = RiskManager(atr_period=10)
        df = make_trending_ohlcv(n_days=30)
        atr = rm10._calc_atr(df)
        assert atr is not None
        assert atr > 0


# ------------------------------------------------------------------
# 移动止损评估
# ------------------------------------------------------------------

class TestStopEvaluation:
    def test_stop_not_triggered_normal(self, rm, long_trend_df):
        """上涨趋势中, 现价远离止损线."""
        stop = rm._evaluate_stop(long_trend_df, "TEST")
        assert stop is not None
        assert not stop.is_triggered
        assert stop.distance_pct > 0
        assert stop.stop_price < stop.current_close

    def test_trailing_high_is_max_close(self, rm, long_trend_df):
        """移动最高价 = lookback 窗口内最高收盘价."""
        stop = rm._evaluate_stop(long_trend_df, "TEST")
        assert stop is not None
        lookback = min(len(long_trend_df), rm.atr_period * 2)
        recent = long_trend_df.tail(lookback)
        expected_high = float(recent["close"].max())
        assert stop.trailing_high == pytest.approx(expected_high, abs=1e-6)

    def test_stop_triggers_when_below(self, rm):
        """现价在止损线下方 → 触发止损."""
        n = 40
        # 构造先涨后暴跌的序列
        prices = [3.0]
        for i in range(1, 20):
            prices.append(prices[-1] * 1.02)  # 前 20 天上涨
        for i in range(20, n):
            prices.append(prices[-1] * 0.93)  # 后 20 天暴跌

        df = pl.DataFrame({
            "symbol": ["CRASH"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": [p * 1.02 for p in prices],
            "low": [p * 0.98 for p in prices],
            "close": prices,
        })
        stop = rm._evaluate_stop(df, "CRASH")
        assert stop is not None
        assert stop.is_triggered, (
            f"close={stop.current_close:.4f} > stop={stop.stop_price:.4f}, "
            f"ATR={stop.atr:.4f}, trail_high={stop.trailing_high:.4f}"
        )

    def test_insufficient_data_returns_none(self, rm):
        """数据不足 → None."""
        df = make_ohlcv(["X"], n_days=10, seed=1)
        stop = rm._evaluate_stop(df, "X")
        assert stop is None

    def test_distance_calculation(self, rm, long_trend_df):
        """距离止损线百分比正确."""
        stop = rm._evaluate_stop(long_trend_df, "TEST")
        assert stop is not None
        expected_dist = (stop.current_close - stop.stop_price) / stop.current_close
        assert stop.distance_pct == pytest.approx(expected_dist, abs=1e-6)

    def test_multiple_symbols(self, rm, multi_etf_df):
        """多标的各自独立计算."""
        for sym in ["ETF_A", "ETF_B", "ETF_C"]:
            stop = rm._evaluate_stop(multi_etf_df, sym)
            assert stop is not None
            assert stop.symbol == sym
            assert stop.atr > 0

    def test_stop_price_non_negative(self, rm):
        """止损线不为负."""
        # 极端情况: trailing_high 很小, ATR 很大
        n = 40
        rng = np.random.default_rng(42)
        prices = 0.01 * np.exp(np.cumsum(rng.normal(0, 0.05, n)))
        df = pl.DataFrame({
            "symbol": ["TINY"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": prices * 1.2,
            "low": prices * 0.8,
            "close": prices,
        })
        stop = rm._evaluate_stop(df, "TINY")
        if stop is not None:
            assert stop.stop_price >= 0.0


# ------------------------------------------------------------------
# 止损线棘轮约束
# ------------------------------------------------------------------

class TestStopRatchet:
    def test_first_entry_uses_current(self, rm):
        """首次持仓 → 使用当前止损线."""
        assert rm.enforce_stop_ratchet(3.50, None) == 3.50

    def test_stop_moves_up(self, rm):
        """止损线上移 (股价上涨)."""
        assert rm.enforce_stop_ratchet(3.80, 3.50) == 3.80

    def test_stop_never_drops(self, rm):
        """止损线不下移 (核心约束)."""
        assert rm.enforce_stop_ratchet(3.20, 3.50) == 3.50

    def test_stop_stays_same(self, rm):
        """相同止损线保持不变."""
        assert rm.enforce_stop_ratchet(3.50, 3.50) == 3.50


# ------------------------------------------------------------------
# 宏观骤降检测
# ------------------------------------------------------------------

class TestPlungeDetection:
    def test_no_plunge_small_drop(self, rm, multi_etf_df):
        """得分小幅下降 → 不触发骤降."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=75.0,
            prev_score=80.0,
            position_cap=0.80,
        )
        assert not report.plunge_detected
        assert report.effective_position_cap == 0.80

    def test_plunge_detected_large_drop(self, rm, multi_etf_df):
        """得分下降 > 30 → 触发骤降, 仓位降至 safe_position_pct."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=35.0,
            prev_score=80.0,
            position_cap=0.80,
        )
        assert report.plunge_detected
        assert report.plunge_score_drop == 45.0
        assert report.effective_position_cap == 0.30

    def test_plunge_cap_already_low(self, rm, multi_etf_df):
        """原仓位已低于安全仓位 → 不进一步降低."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=35.0,
            prev_score=80.0,
            position_cap=0.20,  # 已低于 30%
        )
        assert report.plunge_detected
        assert report.effective_position_cap == 0.20  # 保持原仓位

    def test_exact_threshold_not_triggered(self, rm, multi_etf_df):
        """下降恰好 = 30.0 → 不触发 (> 才触发)."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=50.0,
            prev_score=80.0,
            position_cap=0.80,
        )
        assert not report.plunge_detected

    def test_exact_threshold_plus_epsilon(self, rm, multi_etf_df):
        """下降 30.1 → 触发."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=49.9,
            prev_score=80.0,
            position_cap=0.80,
        )
        assert report.plunge_detected

    def test_score_increase_ignored(self, rm, multi_etf_df):
        """得分上升 → 不触发."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=85.0,
            prev_score=50.0,
            position_cap=0.80,
        )
        assert not report.plunge_detected
        assert report.plunge_score_drop == 0.0


# ------------------------------------------------------------------
# 端到端 evaluate
# ------------------------------------------------------------------

class TestEvaluate:
    def test_normal_market(self, rm):
        """正常市场: 无骤降, 无止损 (用确定性上涨数据)."""
        # 确定性上涨: 不可能触发 trailing stop
        df = make_trending_ohlcv("UP", n_days=40, change_per_day=0.005)
        report = rm.evaluate(
            df,
            current_score=70.0,
            prev_score=72.0,
            position_cap=0.70,
            symbols=["UP"],
        )
        assert isinstance(report, RiskReport)
        assert not report.plunge_detected
        assert len(report.forced_exits) == 0
        assert len(report.stops) == 1
        assert report.effective_position_cap == 0.70

    def test_combined_plunge_and_stops(self, rm):
        """同时骤降 + 止损触发."""
        # 构造暴跌序列触发止损
        n = 40
        prices = [3.0]
        for i in range(1, 20):
            prices.append(prices[-1] * 1.02)
        for i in range(20, n):
            prices.append(prices[-1] * 0.90)

        df = pl.DataFrame({
            "symbol": ["CRASH"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": [p * 1.02 for p in prices],
            "low": [p * 0.98 for p in prices],
            "close": prices,
        })
        report = rm.evaluate(
            df,
            current_score=35.0,
            prev_score=80.0,
            position_cap=0.80,
            symbols=["CRASH"],
        )
        assert report.plunge_detected
        assert report.effective_position_cap == 0.30
        # 暴跌数据应触发止损
        assert len(report.forced_exits) >= 1

    def test_empty_symbols(self, rm):
        """无持仓 → 空报告."""
        df = pl.DataFrame(
            schema={
                "symbol": pl.Utf8, "date": pl.Date,
                "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
            }
        )
        report = rm.evaluate(df, current_score=50, prev_score=50, position_cap=0.50)
        assert len(report.stops) == 0
        assert len(report.forced_exits) == 0

    def test_specific_symbols(self, rm, multi_etf_df):
        """只评估指定 symbols."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=60.0,
            prev_score=60.0,
            position_cap=0.60,
            symbols=["ETF_A"],
        )
        assert len(report.stops) == 1
        assert report.stops[0].symbol == "ETF_A"

    def test_nonexistent_symbol_skipped(self, rm, multi_etf_df):
        """不存在的标的被跳过."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=60.0,
            prev_score=60.0,
            position_cap=0.60,
            symbols=["GHOST"],
        )
        assert len(report.stops) == 0


# ------------------------------------------------------------------
# RiskReport 输出
# ------------------------------------------------------------------

class TestRiskReport:
    def test_plunge_message(self, rm, multi_etf_df):
        """骤降报告含警告信息."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=35.0,
            prev_score=80.0,
            position_cap=0.80,
        )
        assert "骤降" in report.message or "plunge" in report.message.lower() or "⚠" in report.message

    def test_normal_message(self, rm, multi_etf_df):
        """正常报告含 OK 信息."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=72.0,
            prev_score=70.0,
            position_cap=0.70,
        )
        assert len(report.message) > 0

    def test_summary_multiline(self, rm, multi_etf_df):
        """summary() 返回多行字符串."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=72.0,
            prev_score=70.0,
            position_cap=0.70,
        )
        s = report.summary()
        assert len(s.split("\n")) > 1
        for sym in ["ETF_A", "ETF_B", "ETF_C"]:
            assert sym in s

    def test_forced_exits_in_report(self, rm):
        """止损触发出现在 forced_exits."""
        n = 40
        prices = [3.0]
        for i in range(1, 20):
            prices.append(prices[-1] * 1.02)
        for i in range(20, n):
            prices.append(prices[-1] * 0.90)  # 连续暴跌

        df = pl.DataFrame({
            "symbol": ["CRASH"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "high": [p * 1.02 for p in prices],
            "low": [p * 0.98 for p in prices],
            "close": prices,
        })
        report = rm.evaluate(
            df, current_score=70.0, prev_score=70.0,
            position_cap=0.80, symbols=["CRASH"],
        )
        assert "CRASH" in report.forced_exits or not report.stops[0].is_triggered
        # 如果触发, forced_exits 应有 CRASH
        if report.stops and report.stops[0].is_triggered:
            assert "CRASH" in report.forced_exits


# ------------------------------------------------------------------
# 输入校验
# ------------------------------------------------------------------

class TestInputValidation:
    def test_missing_columns(self, rm):
        """缺少必需列 → ValueError."""
        df = pl.DataFrame({
            "symbol": ["X"],
            "date": [date.today()],
            "close": [3.0],
        })
        with pytest.raises(ValueError, match="missing required columns"):
            rm.evaluate(df, current_score=50, prev_score=50, position_cap=0.50)

    def test_all_columns_present(self, rm, multi_etf_df):
        """完整列 → 无异常."""
        report = rm.evaluate(
            multi_etf_df,
            current_score=50, prev_score=50, position_cap=0.50,
        )
        assert isinstance(report, RiskReport)


# ------------------------------------------------------------------
# from_settings 工厂
# ------------------------------------------------------------------

class TestFromSettings:
    def test_full_settings(self):
        """完整设置."""
        settings = {
            "risk_management": {
                "atr_period": 10,
                "atr_multiplier": 1.5,
                "safe_position_pct": 0.25,
                "min_hold_days": 5,
            },
            "macro_timing": {"score_plunge_threshold": 25.0},
        }
        rm = from_settings(settings)
        assert rm.atr_period == 10
        assert rm.atr_multiplier == 1.5
        assert rm.safe_position_pct == 0.25
        assert rm.plunge_threshold == 25.0
        assert rm.min_hold_days == 5

    def test_empty_settings_uses_defaults(self):
        """空设置 → 默认值."""
        rm = from_settings({})
        assert rm.atr_period == 14
        assert rm.atr_multiplier == 2.0
        assert rm.safe_position_pct == 0.30
        assert rm.plunge_threshold == 30.0

    def test_partial_settings(self):
        """部分设置 → 混合默认."""
        settings = {"risk_management": {"atr_period": 20}}
        rm = from_settings(settings)
        assert rm.atr_period == 20
        assert rm.atr_multiplier == 2.0  # default
        assert rm.plunge_threshold == 30.0  # default


# ------------------------------------------------------------------
# 定制参数
# ------------------------------------------------------------------

class TestCustomParameters:
    def test_tighter_stop(self, long_trend_df):
        """更紧的止损 (multiplier=1.0) → 止损线更高."""
        rm_tight = RiskManager(atr_multiplier=1.0)
        rm_loose = RiskManager(atr_multiplier=3.0)
        s_tight = rm_tight._evaluate_stop(long_trend_df, "TEST")
        s_loose = rm_loose._evaluate_stop(long_trend_df, "TEST")
        assert s_tight is not None and s_loose is not None
        assert s_tight.stop_price > s_loose.stop_price, (
            f"Tight stop={s_tight.stop_price:.4f} should > loose={s_loose.stop_price:.4f}"
        )

    def test_shorter_atr_period(self):
        """更短 ATR 周期 → 更敏感."""
        rm_short = RiskManager(atr_period=7)
        df = make_trending_ohlcv(n_days=30)
        atr = rm_short._calc_atr(df)
        assert atr is not None
        assert atr > 0

    def test_custom_plunge_threshold(self, multi_etf_df):
        """自定义骤降阈值."""
        rm_low = RiskManager(plunge_threshold=10.0)
        report = rm_low.evaluate(
            multi_etf_df,
            current_score=70.0, prev_score=85.0,  # 降 15 > 10
            position_cap=0.80,
        )
        assert report.plunge_detected

    def test_safe_position_cap_applied(self, multi_etf_df):
        """安全仓位比例可定制."""
        rm_custom = RiskManager(safe_position_pct=0.20)
        report = rm_custom.evaluate(
            multi_etf_df,
            current_score=30.0, prev_score=80.0,  # 骤降
            position_cap=0.80,
        )
        assert report.plunge_detected
        assert report.effective_position_cap == 0.20
