"""诊断指标函数库测试 (P11-M3-01; 独立期望值手工核对, 不复制实现输出)."""

import math

import numpy as np
import polars as pl
import pytest

from factor_engineering.technical import (
    REQUIRED_PARAM_KEYS,
    calc_amount_activity,
    calc_atr_pct,
    calc_atr_series,
    calc_bollinger,
    calc_ma_deviation,
    calc_ma_slope,
    calc_momentum_12_1,
    calc_percentile_rank,
    calc_ratio_to_baseline,
    calc_return_volatility,
    calc_rsi,
    calc_sma,
    compute_indicators,
    indicator_columns,
)

PARAMS = {
    "ma_windows": [20, 60, 200],
    "ma_slope_window": 20,
    "momentum_lookback": 252,
    "momentum_skip": 21,
    "atr_period": 14,
    "vol_window": 20,
    "vol_pctile_window": 252,
    "vol_pctile_min_periods": 120,
    "rsi_period": 14,
    "boll_window": 20,
    "boll_num_std": 2.0,
    "amount_window": 60,
    "amount_min_periods": 20,
    "atr_ratio_window": 60,
    "atr_ratio_min_periods": 20,
}


def _frame(closes: list[float], amounts: list[float] | None = None) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame({
        "date": pl.date_range(pl.date(2020, 1, 1), pl.date(2030, 1, 1), eager=True)[:n],
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1e6] * n,
        "amount": amounts if amounts is not None else [1e7] * n,
    })


class TestSmaAndDeviation:
    def test_sma_hand_calc(self):
        s = pl.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert calc_sma(s, 3).to_list() == [None, None, 2.0, 3.0, 4.0]

    def test_ma_deviation_hand_calc(self):
        close = pl.Series([10.0, 9.0, 12.0])
        ma = pl.Series([8.0, 10.0, 0.0])
        out = calc_ma_deviation(close, ma).to_list()
        assert out[0] == pytest.approx(0.25)     # 10/8 - 1
        assert out[1] == pytest.approx(-0.1)     # 9/10 - 1
        assert out[2] is None                    # ma=0 → 不可评估

    def test_ma_slope_hand_calc(self):
        ma = pl.Series([10.0, 11.0, 12.0, 15.0])
        out = calc_ma_slope(ma, 2).to_list()
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(12.0 / 10.0 - 1.0)   # 0.2
        assert out[3] == pytest.approx(15.0 / 11.0 - 1.0)


class TestMomentum:
    def test_endpoint_ratio_hand_calc(self):
        closes = [100.0] * 260
        closes[7] = 100.0
        closes[238] = 110.0
        out = calc_momentum_12_1(pl.Series(closes), lookback=252, skip=21)
        assert out[251] is None                    # 历史不足 (需 253 行)
        assert out[259] == pytest.approx(0.10)     # P[238]/P[7]-1 = 110/100-1

    def test_no_subtraction_of_returns(self):
        # 线性价格 1..300: 端点比值 (239/8-1), 而非两个累计收益相减
        closes = [float(i) for i in range(1, 301)]
        out = calc_momentum_12_1(pl.Series(closes), lookback=252, skip=21)
        assert out[259] == pytest.approx(239.0 / 8.0 - 1.0)


class TestAtr:
    def test_atr_and_pct_hand_calc(self):
        n = 20
        closes = [10.0] * n
        high = [10.5] * n
        low = [9.5] * n
        atr = calc_atr_series(pl.Series(high), pl.Series(low), pl.Series(closes), 14)
        values = atr.to_list()
        assert values[13] is None                   # 前 14 行无值
        assert values[14] == pytest.approx(1.0)     # 无跳空 → TR=1.0, Wilder 初始为均值
        assert values[19] == pytest.approx(1.0)
        pct = calc_atr_pct(atr, pl.Series(closes))
        assert pct.to_list()[19] == pytest.approx(0.1)

    def test_insufficient_data_returns_nulls(self):
        closes = [10.0] * 5
        atr = calc_atr_series(
            pl.Series([10.5] * 5), pl.Series([9.5] * 5), pl.Series(closes), 14,
        )
        assert atr.to_list() == [None] * 5


class TestVolatility:
    def test_volatility_hand_calc_first_window(self):
        # 常比增长: 每日对数收益 r 相同 → 样本标准差 = 0
        closes = [100.0 * (1.01 ** i) for i in range(25)]
        vol = calc_return_volatility(pl.Series(closes), 20)
        assert vol.to_list()[-1] == pytest.approx(0.0, abs=1e-12)

    def test_volatility_insufficient(self):
        closes = [10.0, 10.1, 10.2]
        vol = calc_return_volatility(pl.Series(closes), 20)
        assert vol.to_list() == [None, None, None]

    def test_percentile_rank_hand_calc(self):
        s = pl.Series([1.0, 2.0, 3.0, 4.0])
        out = calc_percentile_rank(s, window=4, min_periods=2).to_list()
        assert out[0] is None                      # 样本 1 < min_periods=2
        assert out[1] == pytest.approx(1.0)        # [1,2], 2 为最大 → 2/2
        assert out[2] == pytest.approx(1.0)
        assert out[3] == pytest.approx(1.0)

        desc = pl.Series([4.0, 3.0, 2.0, 1.0])
        out2 = calc_percentile_rank(desc, window=4, min_periods=2).to_list()
        assert out2[3] == pytest.approx(0.25)      # 1 在 [4,3,2,1] 中 ≤ 计数 1/4

    def test_percentile_uses_only_available_samples(self):
        s = pl.Series([1.0, 2.0, 3.0])
        out = calc_percentile_rank(s, window=10, min_periods=2).to_list()
        assert out[1] == pytest.approx(1.0)        # 仅 [1,2] 可得
        assert out[2] == pytest.approx(1.0)


class TestRsi:
    def test_all_gains_is_100(self):
        out = calc_rsi(pl.Series([float(i) for i in range(1, 16)]), 14)
        assert out.to_list()[14] == pytest.approx(100.0)

    def test_all_losses_is_0(self):
        out = calc_rsi(pl.Series([float(i) for i in range(15, 0, -1)]), 14)
        assert out.to_list()[14] == pytest.approx(0.0)

    def test_flat_is_neutral_50(self):
        out = calc_rsi(pl.Series([10.0] * 20), 14)
        assert out.to_list()[14] == pytest.approx(50.0)

    def test_wilder_smoothing_hand_calc(self):
        # period=2, 交替 +1/-1: 手工递推 avg_gain/avg_loss
        closes = pl.Series([10.0, 11.0, 10.0, 11.0, 10.0])
        out = calc_rsi(closes, 2).to_list()
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(50.0)     # gain=loss=0.5 → RS=1
        assert out[3] == pytest.approx(75.0)     # gain=0.75, loss=0.25 → RS=3
        assert out[4] == pytest.approx(37.5)     # gain=0.375, loss=0.625 → RS=0.6

    def test_short_history_all_null(self):
        out = calc_rsi(pl.Series([1.0, 2.0, 3.0]), 14)
        assert out.to_list() == [None, None, None]


class TestBollinger:
    def test_hand_calc(self):
        closes = pl.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        boll = calc_bollinger(closes, 3, 2.0)
        mid = boll["mid"].to_list()
        upper = boll["upper"].to_list()
        lower = boll["lower"].to_list()
        std = math.sqrt(2.0 / 3.0)               # 总体标准差 ddof=0
        assert mid[2] == pytest.approx(2.0)
        assert upper[2] == pytest.approx(2.0 + 2.0 * std)
        assert lower[2] == pytest.approx(2.0 - 2.0 * std)
        assert boll["position"].to_list()[2] == pytest.approx(
            (3.0 - lower[2]) / (upper[2] - lower[2])
        )

    def test_zero_width_position_is_null_not_fake(self):
        boll = calc_bollinger(pl.Series([10.0] * 25), 20, 2.0)
        assert boll["position"].to_list()[-1] is None    # 带宽 0 → 不可评估
        assert boll["bandwidth"].to_list()[-1] == pytest.approx(0.0)
        assert boll["upper"].to_list()[-1] == pytest.approx(10.0)


class TestActivity:
    def test_amount_ratio_excludes_today(self):
        amounts = [100.0] * 6 + [1000.0]
        out = calc_amount_activity(pl.Series(amounts), 5, min_periods=3)
        assert out.to_list()[-1] == pytest.approx(10.0)  # 1000 / mean(前5日=100)

    def test_amount_ratio_baseline_short(self):
        out = calc_amount_activity(pl.Series([100.0, 100.0, 100.0]), 5, min_periods=3)
        assert out.to_list()[2] is None                  # 基准样本 2 < 3

    def test_ratio_to_baseline(self):
        series = pl.Series([1.0] * 6 + [1.5])
        out = calc_ratio_to_baseline(series, 5, min_periods=3)
        assert out.to_list()[-1] == pytest.approx(1.5)


class TestComputeIndicators:
    def test_missing_params_raises(self):
        with pytest.raises(ValueError, match="参数缺失"):
            compute_indicators(_frame([1.0] * 30), {"ma_windows": [20]})

    def test_columns_match_indicator_columns(self):
        closes = [100.0 + i * 0.1 for i in range(300)]
        df = compute_indicators(_frame(closes), PARAMS)
        expected = indicator_columns(PARAMS)
        for col in expected:
            assert col in df.columns, col

    def test_long_window_null_until_enough_history(self):
        closes = [100.0 + i * 0.1 for i in range(300)]
        df = compute_indicators(_frame(closes), PARAMS)
        ma200 = df["ma200"].to_list()
        assert ma200[198] is None
        assert ma200[199] is not None
        mom = df["momentum_12_1"].to_list()
        assert mom[251] is None
        assert mom[252] is not None                       # shift(252) 需 253 行

    def test_zero_volatility_no_div_by_zero(self):
        df = compute_indicators(_frame([10.0] * 300), PARAMS)
        for col in indicator_columns(PARAMS):
            values = df[col].to_list()
            assert not any(
                isinstance(v, float) and (math.isinf(v) or math.isnan(v))
                for v in values
            ), col
        assert df["boll_pos"].to_list()[-1] is None       # 带宽 0 → 位置空
        assert df["vol20"].to_list()[-1] == pytest.approx(0.0)

    def test_short_history_no_fake_values(self):
        df = compute_indicators(_frame([10.0 + i * 0.05 for i in range(10)]), PARAMS)
        assert df["ma200"].to_list() == [None] * 10
        assert df["momentum_12_1"].to_list() == [None] * 10
        assert df["vol_pctile"].to_list() == [None] * 10

    def test_empty_frame_passthrough(self):
        empty = pl.DataFrame(schema={
            "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
            "amount": pl.Float64,
        })
        assert compute_indicators(empty, PARAMS).is_empty()

    def test_params_keys_complete(self):
        assert set(REQUIRED_PARAM_KEYS) <= set(PARAMS)

    def test_numpy_series_input_consistency(self):
        rng = np.random.default_rng(42)
        closes = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 280)))
        df = compute_indicators(_frame(closes), PARAMS)
        # 最后一行的 MA20 与手工均值一致
        expected_ma20 = sum(df["close"].to_list()[-20:]) / 20
        assert df["ma20"].to_list()[-1] == pytest.approx(expected_ma20)
