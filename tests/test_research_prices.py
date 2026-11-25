"""研究价格序列测试 (P1-M1-02; 独立手工期望值, 不以实现自证)."""

from datetime import date, timedelta

import polars as pl
import pytest

from data_pipeline.research_prices import (
    build_research_series,
    parse_cash_per_share,
    prepare_events,
    research_price_version,
)


def _prices(closes: list[float], start: date = date(2024, 1, 1)) -> pl.DataFrame:
    days = [start + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame({
        "date": days,
        "open": closes,
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100.0] * len(closes),
        "amount": [1000.0] * len(closes),
    })


def _events(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _series(
    closes: list[float], events: list[dict], *, threshold: float = 0.15,
) -> pl.DataFrame:
    prepared = prepare_events(_events(events))
    prices = _prices(closes)
    return build_research_series(
        prices, prepared.get("510300", {}), jump_threshold=threshold,
    )


class TestSplitAndMerge:
    def test_split_makes_returns_continuous(self):
        series = _series(
            [10.0, 10.0, 5.0, 5.5],
            [{"symbol": "510300", "event_date": date(2024, 1, 3),
              "event_type": "split_official", "ratio": 2.0, "method": None}],
        )
        # 1:2 拆分 (ratio=2): 第 3 天价格减半但份额翻倍 → 总收益 0; 第 4 天 +10%
        assert series["tr_index"].to_list() == pytest.approx([100.0, 100.0, 100.0, 110.0])
        # 拆分后复权价: 5×2=10, 5.5×2=11
        assert series["adj_close"].to_list() == pytest.approx([10.0, 10.0, 10.0, 11.0])
        assert series["quality_flag"].to_list() == [
            "first", "normal", "verified_event", "normal",
        ]

    def test_merge_ratio_below_one(self):
        series = _series(
            [5.0, 5.0, 10.0],
            [{"symbol": "510300", "event_date": date(2024, 1, 3),
              "event_type": "merge_official", "ratio": 0.5, "method": None}],
        )
        # 1:0.5 合并: 价格 ×2, 份额 ×0.5 → 总收益 0; 复权价 10×0.5=5
        assert series["tr_index"].to_list() == pytest.approx([100.0, 100.0, 100.0])
        assert series["adj_close"].to_list() == pytest.approx([5.0, 5.0, 5.0])

    def test_split_and_dividend_same_day(self):
        series = _series(
            [10.0, 10.0, 4.75],
            [{
                "symbol": "510300", "event_date": date(2024, 1, 3),
                "event_type": "split_official", "ratio": 2.0,
                "method": '{"cash_per_share": 0.5}',
            }],
        )
        # (4.75×2 + 0.5)/10 - 1 = 0 → 总收益 0
        assert series["tr_index"].to_list() == pytest.approx([100.0, 100.0, 100.0])
        assert series["adj_close"][2] == pytest.approx(9.5)


class TestDividend:
    def test_dividend_uses_cash_not_ratio(self):
        series = _series(
            [10.0, 10.0, 9.5],
            [{
                "symbol": "510300", "event_date": date(2024, 1, 3),
                "event_type": "dividend_official", "ratio": 1.371,
                "method": '{"cash_per_share": 0.5, "official_ratio": null}',
            }],
        )
        # (9.5 + 0.5)/10 - 1 = 0; 分红不改变 split_factor
        assert series["tr_index"].to_list() == pytest.approx([100.0, 100.0, 100.0])
        assert series["adj_close"].to_list() == pytest.approx([10.0, 10.0, 9.5])
        assert series["quality_flag"][2] == "verified_event"


class TestQualityFlags:
    def test_unverified_jump_is_flagged_not_adjusted(self):
        series = _series([10.0, 10.0, 8.0], [])
        assert series["quality_flag"].to_list() == ["first", "normal", "unverified_jump"]
        assert series["tr_index"][2] == pytest.approx(80.0)
        assert series["adj_close"][2] == pytest.approx(8.0)

    def test_below_threshold_is_normal(self):
        series = _series([10.0, 10.0, 9.0], [], threshold=0.15)
        assert series["quality_flag"][2] == "normal"


class TestEventParsing:
    def test_parse_cash_per_share(self):
        assert parse_cash_per_share('{"cash_per_share": 0.35}') == pytest.approx(0.35)
        assert parse_cash_per_share('{"cash_per_share": null}') is None
        assert parse_cash_per_share("not-json") is None
        assert parse_cash_per_share(None) is None

    def test_prepare_events_classifies_kind(self):
        prepared = prepare_events(_events([
            {"symbol": "510300", "event_date": date(2024, 1, 3),
             "event_type": "split_official", "ratio": 2.0, "method": None},
            {"symbol": "512690", "event_date": date(2024, 1, 4),
             "event_type": "dividend_official", "ratio": 1.371,
             "method": '{"cash_per_share": 0.35}'},
        ]))
        assert prepared["510300"][date(2024, 1, 3)]["kind"] == "split"
        assert prepared["512690"][date(2024, 1, 4)]["kind"] == "dividend"
        assert prepared["512690"][date(2024, 1, 4)]["cash_per_share"] == pytest.approx(0.35)


class TestVersion:
    def _events(self, ratio: float = 2.0) -> pl.DataFrame:
        return _events([{
            "symbol": "510300", "event_date": date(2024, 1, 3),
            "event_type": "split_official", "ratio": ratio, "method": None,
        }])

    def test_version_is_deterministic_and_sensitive(self):
        events = self._events()
        v1 = research_price_version(events, date(2024, 1, 31), "abc123")
        v2 = research_price_version(events, date(2024, 1, 31), "abc123")
        assert v1 == v2
        assert v1.startswith("rp:v1:")
        assert research_price_version(self._events(3.0), date(2024, 1, 31), "abc123") != v1
        assert research_price_version(events, date(2024, 2, 29), "abc123") != v1
        assert research_price_version(events, date(2024, 1, 31), "def456") != v1
