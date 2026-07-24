"""宏观择时模块单元测试.

验证: 趋势动量打分、市场宽度打分、复合得分、仓位映射、骤降检测.
"""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from strategies.macro_timing import MacroScore, MacroTiming


# ------------------------------------------------------------------
# 辅助: 生成合成 OHLCV
# ------------------------------------------------------------------

def make_price_series(
    n_days: int,
    trend: str = "ranging",
    start_price: float = 3.5,
    seed: int = 42,
) -> list[float]:
    """生成指定趋势的价格序列 (确定性线性趋势 + 小噪声).

    Args:
        n_days: 交易日数
        trend: "bull" | "bear" | "ranging"
        start_price: 起始价格
    """
    rng = np.random.default_rng(seed)

    if trend == "bull":
        # 线性上行 + 小噪声: 确保 MA20 >> MA60
        drift = np.linspace(0, 0.60, n_days)  # 60% 涨幅
    elif trend == "bear":
        drift = np.linspace(0, -0.50, n_days)  # 50% 跌幅
    else:
        drift = np.linspace(0, 0.05, n_days)   # 5% 微涨 (震荡)

    noise = rng.normal(0, 0.008, n_days)  # 日噪声 0.8%
    prices = start_price * (1 + drift + noise)
    return prices.tolist()


def make_ohlcv_df(
    n_days: int,
    symbol: str = "510300",
    trend: str = "ranging",
    start_price: float = 3.5,
    start_date: date = date(2023, 1, 4),
) -> pl.DataFrame:
    """构造单标的 OHLCV DataFrame."""
    prices = make_price_series(n_days, trend, start_price)
    rng = np.random.default_rng(42)
    dates = []
    d = start_date
    while len(dates) < n_days:
        if d.weekday() < 5:  # 工作日
            dates.append(d)
        d += timedelta(days=1)

    n = len(prices)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates[:n],
        "open": [p * (1 + rng.uniform(-0.005, 0.005)) for p in prices],
        "high": [p * (1 + abs(rng.uniform(0, 0.015))) for p in prices],
        "low": [p * (1 - abs(rng.uniform(0, 0.015))) for p in prices],
        "close": prices,
        "volume": [1e7] * n,
        "amount": [p * 1e7 for p in prices],
    })


def make_universe_df(
    benchmark_df: pl.DataFrame,
    n_etfs: int = 20,
    above_ma20_fraction: float = 0.6,
) -> pl.DataFrame:
    """构造多标的 universe DataFrame (用于市场宽度测试).

    Args:
        benchmark_df: 基准 ETF 的 OHLCV (日期和价格级别参考)
        n_etfs: ETF 总数
        above_ma20_fraction: 收盘价 > MA20 的比例
    """
    rng = np.random.default_rng(99)
    dfs = []

    # 基准自身
    dfs.append(benchmark_df)

    # 其他标的
    for i in range(2, n_etfs + 1):
        sym = f"ETF{i:03d}"
        df = benchmark_df.with_columns([
            pl.lit(sym).alias("symbol"),
            pl.col("close") * (1 + rng.uniform(-0.1, 0.1, 1)[0]) * rng.uniform(0.95, 1.05, 1)[0],
        ])
        dfs.append(df)

    return pl.concat(dfs)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def mt():
    return MacroTiming(ma_short=20, ma_long=60)


@pytest.fixture
def bull_df():
    """120 天牛市数据 (MA20 在 MA60 上方)."""
    return make_ohlcv_df(120, trend="bull", start_price=3.0)


@pytest.fixture
def bear_df():
    """120 天熊市数据."""
    return make_ohlcv_df(120, trend="bear", start_price=5.0)


@pytest.fixture
def ranging_df():
    """120 天震荡市数据."""
    return make_ohlcv_df(120, trend="ranging", start_price=3.5)


@pytest.fixture
def short_df():
    """不足 MA60 的短数据."""
    return make_ohlcv_df(30, trend="ranging")


# ------------------------------------------------------------------
# 趋势动量打分
# ------------------------------------------------------------------

class TestTrendScore:
    def test_bull_market_high_score(self, mt, bull_df):
        """牛市: 趋势得分应 > 60."""
        score = mt._calc_trend_score(bull_df)
        assert score > 60.0, f"Bull market score too low: {score}"

    def test_bear_market_low_score(self, mt, bear_df):
        """熊市: 趋势得分应 < 40."""
        score = mt._calc_trend_score(bear_df)
        assert score < 40.0, f"Bear market score too high: {score}"

    def test_ranging_mid_score(self, mt, ranging_df):
        """震荡: 趋势得分应在 30-70 之间."""
        score = mt._calc_trend_score(ranging_df)
        assert 30.0 < score < 70.0, f"Ranging score out of range: {score}"

    def test_short_data_returns_neutral(self, mt, short_df):
        """数据不足 MA60 → 返回 50 (中性)."""
        score = mt._calc_trend_score(short_df)
        assert score == 50.0

    def test_empty_data(self, mt):
        assert mt._calc_trend_score(pl.DataFrame()) == 50.0

    def test_score_in_range(self, mt, bull_df):
        """得分始终在 0-100 范围内."""
        for trend in ["bull", "bear", "ranging"]:
            df = make_ohlcv_df(120, trend=trend)
            score = mt._calc_trend_score(df)
            assert 0.0 <= score <= 100.0, f"{trend}: {score}"


# ------------------------------------------------------------------
# 市场宽度打分
# ------------------------------------------------------------------

class TestBreadthScore:
    def test_all_above_ma20(self, mt):
        """所有标的站上 MA20 → 宽度 100."""
        # 构造强制所有标的 close > MA20 的数据
        n = 100
        prices = list(range(9000, 9000 + n))  # 严格递增 → MA20 始终低于当前价
        df = pl.DataFrame({
            "symbol": ["T001"] * n,
            "date": [date(2023, 1, 4) + timedelta(days=i) for i in range(n)],
            "close": [float(p) / 1000 for p in prices],
            "open": [float(p) / 1000 for p in prices],
            "high": [float(p) / 1000 for p in prices],
            "low": [float(p) / 1000 for p in prices],
            "volume": [1e6] * n,
            "amount": [1e6] * n,
        })
        score = mt._calc_breadth_score(df)
        assert score == 100.0, f"Expected 100, got {score}"

    def test_none_above_ma20(self, mt):
        """所有标的低于 MA20 → 宽度接近 0."""
        n = 100
        prices = list(range(9000, 9000 - n, -1))  # 严格递减
        df = pl.DataFrame({
            "symbol": ["T001"] * n,
            "date": [date(2023, 1, 4) + timedelta(days=i) for i in range(n)],
            "close": [float(p) / 1000 for p in prices],
            "open": [float(p) / 1000 for p in prices],
            "high": [float(p) / 1000 for p in prices],
            "low": [float(p) / 1000 for p in prices],
            "volume": [1e6] * n,
            "amount": [1e6] * n,
        })
        score = mt._calc_breadth_score(df)
        assert score == 0.0, f"Expected 0, got {score}"

    def test_empty_universe(self, mt):
        assert mt._calc_breadth_score(pl.DataFrame()) == 50.0


# ------------------------------------------------------------------
# 仓位映射
# ------------------------------------------------------------------

class TestPositionCap:
    def test_low_score_no_position(self, mt):
        """得分 ≤ 20 → 仓位 0%."""
        assert mt.score_to_position_cap(0) == 0.0
        assert mt.score_to_position_cap(10) == 0.0
        assert mt.score_to_position_cap(20) == 0.0

    def test_high_score_full_position(self, mt):
        """得分 100 → 仓位 100%."""
        assert mt.score_to_position_cap(100) == 1.0

    def test_mid_score(self, mt):
        """中等得分 → 中等仓位."""
        cap = mt.score_to_position_cap(50)
        assert 0.4 < cap < 0.7, f"cap={cap}"

    def test_monotonic(self, mt):
        """仓位映射应单调递增."""
        caps = [mt.score_to_position_cap(s) for s in range(0, 101, 5)]
        for i in range(len(caps) - 1):
            assert caps[i] <= caps[i + 1], f"Not monotonic at {i*5}"

    def test_bounds(self, mt):
        """所有输入 → 输出在 [0, 1]."""
        for s in [0, 10, 25, 50, 75, 90, 100]:
            cap = mt.score_to_position_cap(s)
            assert 0.0 <= cap <= 1.0, f"score={s} → cap={cap}"


# ------------------------------------------------------------------
# 端到端 evaluate
# ------------------------------------------------------------------

class TestEvaluate:
    def test_bull_market(self, mt, bull_df):
        """牛市: 高得分 + 高仓位上限."""
        uni = make_universe_df(bull_df.tail(60), above_ma20_fraction=0.8)
        result = mt.evaluate(bull_df, uni)
        assert result.composite_score > 50
        assert result.position_cap > 0.5
        assert result.regime == "bull"

    def test_bear_market(self, mt, bear_df):
        """熊市: 低得分 + 低仓位上限."""
        uni = make_universe_df(bear_df.tail(60), above_ma20_fraction=0.2)
        result = mt.evaluate(bear_df, uni)
        assert result.composite_score < 50
        assert result.position_cap < 0.5
        assert result.regime == "bear"

    def test_result_structure(self, mt, bull_df):
        """验证返回结构完整性."""
        uni = make_universe_df(bull_df.tail(60))
        result = mt.evaluate(bull_df, uni)
        assert isinstance(result, MacroScore)
        assert 0 <= result.trend_score <= 100
        assert 0 <= result.breadth_score <= 100
        assert 0 <= result.composite_score <= 100
        assert 0.0 <= result.position_cap <= 1.0
        assert result.regime in ("bull", "bear", "ranging", "unknown")


# ------------------------------------------------------------------
# 骤降检测
# ------------------------------------------------------------------

class TestPlungeDetection:
    def test_no_plunge(self):
        s1 = MacroScore(trend_score=80, breadth_score=80, composite_score=80, position_cap=0.9,
                        regime="bull", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.8)
        s2 = MacroScore(trend_score=78, breadth_score=79, composite_score=79, position_cap=0.9,
                        regime="bull", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.8)
        assert not s2.is_plunge(s1.composite_score)

    def test_plunge_detected(self):
        s1 = MacroScore(trend_score=80, breadth_score=80, composite_score=80, position_cap=0.9,
                        regime="bull", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.8)
        s2 = MacroScore(trend_score=20, breadth_score=30, composite_score=25, position_cap=0.2,
                        regime="bear", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.3)
        # 80 → 25 = 下降 55 分 > 30 → 触发
        assert s2.is_plunge(s1.composite_score)

    def test_plunge_custom_threshold(self):
        s1 = MacroScore(trend_score=50, breadth_score=50, composite_score=50, position_cap=0.5,
                        regime="ranging", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.5)
        s2 = MacroScore(trend_score=38, breadth_score=38, composite_score=38, position_cap=0.38,
                        regime="ranging", ma_short=3.9, ma_long=3.7, latest_close=4.0, breadth_pct=0.38)
        # 下降 12 < 15(自定义) → 不触发
        assert not s2.is_plunge(s1.composite_score, threshold=15.0)
        # 下降 12 > 10(自定义) → 触发
        assert s2.is_plunge(s1.composite_score, threshold=10.0)
