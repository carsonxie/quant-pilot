"""产品对照与相关性诊断 (M3-06) 单元测试.

覆盖: 相关矩阵独立期望值 (相同序列=1, 镜像收益=-1)、样本不足进入 skipped、
同指数分组、对照表字段与样本标注.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from strategies import product_compare

END = date(2026, 9, 11)


def _days(n: int, end: date = END) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _series(symbol: str, closes: list[float], amount: float = 8e6) -> pl.DataFrame:
    dates = _days(len(closes))
    return pl.DataFrame({
        "symbol": [symbol] * len(closes), "date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1e6] * len(closes), "amount": [amount] * len(closes),
    })


def _corr(matrix: pl.DataFrame, row_symbol: str, col_symbol: str) -> float:
    return float(matrix.filter(pl.col("symbol") == row_symbol)[col_symbol][0])


class TestCorrelationReport:
    def test_identical_series_corr_one(self):
        closes = [3.0 * (1.001 ** i) for i in range(80)]
        df = pl.concat([_series("A", closes), _series("B", closes)])
        report = product_compare.correlation_report(df, window_days=250, min_samples=60)

        assert set(report.symbols) == {"A", "B"}
        assert _corr(report.matrix, "A", "A") == pytest.approx(1.0)
        assert _corr(report.matrix, "A", "B") == pytest.approx(1.0)
        assert report.samples["A"] >= 60

    def test_mirrored_returns_corr_minus_one(self):
        rets = [0.001 if i % 2 == 0 else -0.001 for i in range(79)]
        close_a = [3.0]
        for r in rets:
            close_a.append(close_a[-1] * math.exp(r))
        close_b = [3.0]
        for r in rets:
            close_b.append(close_b[-1] * math.exp(-r))
        df = pl.concat([_series("A", close_a), _series("B", close_b)])
        report = product_compare.correlation_report(df, window_days=250, min_samples=60)

        assert _corr(report.matrix, "A", "B") == pytest.approx(-1.0, abs=1e-9)

    def test_short_series_goes_to_skipped(self):
        long_closes = [3.0 * (1.001 ** i) for i in range(80)]
        short_closes = [3.0] * 30
        df = pl.concat([
            _series("A", long_closes), _series("B", short_closes),
        ])
        report = product_compare.correlation_report(df, min_samples=60)

        assert "B" in report.skipped
        assert report.matrix.is_empty()
        assert report.summary()  # 可读摘要 (含跳过原因)

    def test_empty_input(self):
        report = product_compare.correlation_report(pl.DataFrame())
        assert report.matrix.is_empty()
        assert report.n_days == 0


class TestSameIndexGroups:
    def test_groups_with_two_or_more(self):
        instruments = pl.DataFrame([
            {"symbol": "513100", "name": "纳指ETF", "index_name": "纳斯达克100"},
            {"symbol": "159941", "name": "纳指ETF(广发)", "index_name": "纳斯达克100"},
            {"symbol": "518880", "name": "黄金ETF", "index_name": "黄金现货AU9999"},
            {"symbol": "510300", "name": "沪深300ETF", "index_name": None},
        ])
        groups = product_compare.same_index_groups(instruments)
        assert groups == {"纳斯达克100": ["159941", "513100"]}

    def test_empty_instruments(self):
        assert product_compare.same_index_groups(pl.DataFrame()) == {}


class TestCompareProducts:
    def test_compare_table_reports_window_and_samples(self):
        instruments = pl.DataFrame([
            {"symbol": "513100", "name": "纳指ETF", "index_name": "纳斯达克100"},
            {"symbol": "159941", "name": "纳指ETF(广发)", "index_name": "纳斯达克100"},
        ])
        closes_a = [3.0 * (1.001 ** i) for i in range(80)]
        closes_b = [3.0 * (1.0008 ** i) for i in range(80)]
        df = pl.concat([
            _series("513100", closes_a, amount=1e8),
            _series("159941", closes_b, amount=5e7),
        ])
        table = product_compare.compare_products(
            df, instruments, "纳斯达克100", window_days=250,
        )
        assert table.height == 2
        row = table.filter(pl.col("symbol") == "513100").row(0, named=True)
        assert row["n_days"] == 80
        assert row["annualized_vol"] is not None
        assert row["avg_amount_20d"] == pytest.approx(1e8)
        assert row["note"] is None  # 样本充足

    def test_unknown_index_returns_empty(self):
        instruments = pl.DataFrame([
            {"symbol": "513100", "name": "纳指ETF", "index_name": "纳斯达克100"},
        ])
        assert product_compare.compare_products(
            pl.DataFrame(), instruments, "不存在指数",
        ).is_empty()
