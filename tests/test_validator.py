"""数据质量护栏单元测试.

验证 5 项检查: 负价格、涨跌幅异常、缺失值、时间戳断点、行数偏离.
"""

from datetime import date, timedelta

import polars as pl
import pytest

from data_pipeline.processors.validator import (
    DataQualityError,
    _check_daily_change,
    _check_missing_values,
    _check_negative_prices,
    _check_row_count,
    _check_timestamp_continuity,
    validate_ohlcv,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def clean_df():
    """5 个交易日的干净 OHLCV 数据."""
    dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]
    return pl.DataFrame({
        "symbol": ["510300"] * 5,
        "date": dates,
        "open": [3.5, 3.52, 3.51, 3.55, 3.54],
        "high": [3.55, 3.53, 3.54, 3.57, 3.56],
        "low": [3.48, 3.50, 3.49, 3.53, 3.52],
        "close": [3.52, 3.51, 3.55, 3.54, 3.56],
        "volume": [1e7, 1.1e7, 1.2e7, 1.15e7, 1.3e7],
        "amount": [3.5e7, 3.8e7, 4.1e7, 3.9e7, 4.5e7],
    })


# ------------------------------------------------------------------
# 负价格拦截 (阻断级)
# ------------------------------------------------------------------

class TestNegativePrices:
    def test_clean_data_no_issues(self, clean_df):
        assert _check_negative_prices(clean_df) == []

    def test_negative_open(self, clean_df):
        df = clean_df.with_columns(pl.when(pl.arange(0, 5) == 2)
                                   .then(pl.lit(-0.5))
                                   .otherwise(pl.col("open"))
                                   .alias("open"))
        issues = _check_negative_prices(df)
        assert len(issues) == 1
        assert issues[0].severity == "critical"
        assert issues[0].check == "negative_price"
        assert "open" in issues[0].detail

    def test_negative_close(self, clean_df):
        df = clean_df.with_columns(close=pl.lit(-1.0))
        issues = _check_negative_prices(df)
        assert len(issues) == 1
        assert "close" in issues[0].detail
        assert issues[0].sample_rows == 5

    def test_multiple_negative_columns(self, clean_df):
        df = clean_df.with_columns(high=pl.lit(-1.0), low=pl.lit(-2.0))
        issues = _check_negative_prices(df)
        assert len(issues) == 2  # high + low


# ------------------------------------------------------------------
# 涨跌幅异常拦截 (阻断级)
# ------------------------------------------------------------------

class TestDailyChange:
    def test_normal_changes_no_issue(self, clean_df):
        assert _check_daily_change(clean_df) == []

    def test_spike_detected(self, clean_df):
        """注入单日 25% 暴涨."""
        spikes = pl.DataFrame({
            "symbol": ["510300"] * 5,
            "date": [date(2024, 1, 2) + timedelta(days=i) for i in range(5)],
            "close": [3.52, 3.51, 3.55, 3.54, 4.50],  # last day: +27% jump
        })
        # 只用 close 列测试 (简化)
        df = clean_df.drop("close").join(
            spikes.select(["symbol", "date", "close"]),
            on=["symbol", "date"],
        )
        issues = _check_daily_change(df)
        assert len(issues) == 1
        assert issues[0].severity == "critical"
        assert issues[0].check == "extreme_daily_change"

    def test_custom_threshold(self, clean_df):
        """更紧的限制 (0.1%) + 零容差 → 触发阻断级."""
        issues = _check_daily_change(clean_df, default_limit=0.001, buffer=0.0)
        assert len(issues) >= 1
        assert all(i.severity == "critical" for i in issues)
        assert issues[0].check == "extreme_daily_change"


class TestProductAwareDailyChange:
    """C10: 产品感知涨跌幅 — 10%/20% 板与超限容差分级."""

    @staticmethod
    def _df_with_move(symbol: str, move: float):
        base = 3.0
        return pl.DataFrame({
            "symbol": [symbol, symbol],
            "date": [date(2024, 1, 2), date(2024, 1, 3)],
            "close": [base, base * (1 + move)],
        })

    def test_within_10pct_limit_passes(self):
        assert _check_daily_change(self._df_with_move("510300", 0.08)) == []

    def test_within_20pct_board_limit_passes(self):
        limits = {"159915": 0.20}
        assert _check_daily_change(
            self._df_with_move("159915", 0.195), limits=limits,
        ) == []

    def test_small_overshoot_is_warning_not_block(self):
        """10% 板 +10.8% (容差内) → 警告复核, 不阻断."""
        issues = _check_daily_change(self._df_with_move("510300", 0.108))
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert issues[0].check == "daily_change_review"

    def test_large_overshoot_is_critical(self):
        issues = _check_daily_change(self._df_with_move("510300", 0.25))
        assert len(issues) == 1
        assert issues[0].severity == "critical"

    def test_split_like_artifact_critical(self):
        """未调整权益事件 (-50% 拆分) 被隔离为 critical."""
        issues = _check_daily_change(self._df_with_move("512480", -0.50))
        assert any(i.severity == "critical" for i in issues)

    def test_20pct_board_zones(self):
        limits = {"159915": 0.20}
        issues = _check_daily_change(self._df_with_move("159915", 0.21), limits=limits)
        assert len(issues) == 1 and issues[0].severity == "warning"
        issues = _check_daily_change(self._df_with_move("159915", 0.45), limits=limits)
        assert any(i.severity == "critical" for i in issues)

    def test_validate_ohlcv_passes_limits_through(self):
        """validate_ohlcv 的 price_limits 覆盖默认 10% 板."""
        df = self._df_with_move("159915", 0.15)  # +15%
        assert any(i.severity == "critical" for i in validate_ohlcv(df, critical_only=True))
        assert validate_ohlcv(
            df, critical_only=True, price_limits={"159915": 0.20},
        ) == []


# ------------------------------------------------------------------
# 缺失值检查 (警告级)
# ------------------------------------------------------------------

class TestMissingValues:
    def test_no_missing(self, clean_df):
        assert _check_missing_values(clean_df) == []

    def test_high_missing_pct(self, clean_df):
        df = clean_df.with_columns(
            volume=pl.when(pl.arange(0, 5) < 4)
            .then(pl.col("volume"))
            .otherwise(None)
        )
        # 1/5 = 20% > 5% threshold
        issues = _check_missing_values(df)
        assert len(issues) >= 1
        assert any(i.check == "excessive_missing" for i in issues)

    def test_missing_column_warns(self, clean_df):
        df = clean_df.drop("volume")
        issues = _check_missing_values(df)
        assert any(i.check == "missing_column" and "volume" in i.detail for i in issues)


# ------------------------------------------------------------------
# 时间戳连续性 (警告级)
# ------------------------------------------------------------------

class TestTimestampContinuity:
    def test_continuous_dates_no_issue(self, clean_df):
        assert _check_timestamp_continuity(clean_df) == []

    def test_large_gap_detected(self, clean_df):
        """故意制造 10 天断点."""
        dates = [
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
            # 断点: 跳到 1/15
            date(2024, 1, 15), date(2024, 1, 16),
        ]
        df = pl.DataFrame({
            "symbol": ["510300"] * 5,
            "date": dates,
            "close": [3.5, 3.51, 3.52, 3.53, 3.54],
        })
        issues = _check_timestamp_continuity(df)
        assert len(issues) >= 1
        assert issues[0].check == "timestamp_gap"

    def test_weekend_gap_allowed(self, clean_df):
        """周五→周一 (3天) 不应报警."""
        dates = [
            date(2024, 1, 5),   # 周五
            date(2024, 1, 8),   # 周一
            date(2024, 1, 9),
        ]
        df = pl.DataFrame({
            "symbol": ["510300"] * 3,
            "date": dates,
            "close": [3.50, 3.51, 3.52],
        })
        issues = _check_timestamp_continuity(df, max_gap_days=5)
        assert issues == []  # 3天间隔不应触发


# ------------------------------------------------------------------
# 行数偏离检查 (警告级)
# ------------------------------------------------------------------

class TestRowCountDeviation:
    def test_normal_count_no_issue(self):
        df = pl.DataFrame({"date": [date(2024, 1, i) for i in range(1, 6)], "x": range(5)})
        stats = {"avg_row_count": 5.0, "std_row_count": 1.0}
        assert _check_row_count(df, stats) == []

    def test_extreme_deviation(self):
        df = pl.DataFrame({
            "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(99)],
            "x": range(99),
        })
        stats = {"avg_row_count": 5.0, "std_row_count": 1.0}
        # 99 vs expected 5±1 → 偏离 (99-5)/1 = 94σ → 触发
        issues = _check_row_count(df, stats)
        assert len(issues) == 1
        assert issues[0].check == "row_count_deviation"
        assert issues[0].severity == "warning"

    def test_insufficient_stats(self):
        """统计数据不足时跳过检查."""
        df = pl.DataFrame({"date": [date(2024, 1, 1)], "x": [1]})
        assert _check_row_count(df, {}) == []
        assert _check_row_count(df, {"avg_row_count": None, "std_row_count": 0}) == []


# ------------------------------------------------------------------
# 端到端入口
# ------------------------------------------------------------------

class TestValidateOHLCV:
    def test_clean_data_no_issues(self, clean_df):
        assert validate_ohlcv(clean_df) == []

    def test_negative_price_blocked(self, clean_df):
        df = clean_df.with_columns(close=pl.lit(-1.0))
        issues = validate_ohlcv(df)
        assert any(i.severity == "critical" for i in issues)

    def test_critical_only_mode(self, clean_df):
        """critical_only=True 跳过高开销检查."""
        df = clean_df.with_columns(close=pl.lit(-1.0))
        issues = validate_ohlcv(df, critical_only=True)
        # 应该只包含 critical 检查
        assert all(i.severity == "critical" for i in issues)
        # 不应该包含 missing values 警告
        assert not any(i.check == "excessive_missing" for i in issues)

    def test_data_quality_error(self, clean_df):
        """DataQualityError 异常包含所有 issue 信息."""
        df = clean_df.with_columns(close=pl.lit(-5.0))
        issues = validate_ohlcv(df)
        criticals = [i for i in issues if i.severity == "critical"]
        err = DataQualityError(criticals)
        assert "negative_price" in str(err)

    def test_with_historical_stats(self, clean_df):
        stats = {"avg_row_count": 5.0, "std_row_count": 1.0}
        issues = validate_ohlcv(clean_df, historical_stats=stats)
        # 干净数据 + 正常行数 = 无问题
        assert all(i.severity != "critical" for i in issues)
