"""基本面/ETF 数据层测试 (P11-M4; 合成原始帧, 手工期望值, 不依赖网络)."""

from datetime import date, datetime

import pandas as pd
import pytest

from data_pipeline.fundamental_data import (
    _get_cached,
    build_stock_extract,
    parse_date,
    parse_fee,
    parse_float,
    parse_inception,
    parse_overview_frame,
    parse_premium_from_snapshot,
    parse_scale,
    parse_spread_from_category,
    reset_snapshot_caches,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    reset_snapshot_caches()
    yield
    reset_snapshot_caches()


class TestParseBasics:
    def test_parse_float(self):
        assert parse_float(1.5) == 1.5
        assert parse_float("1234.5") == 1234.5
        assert parse_float("1,234.5") == 1234.5
        assert parse_float("0.33%") == pytest.approx(0.0033)
        assert parse_float("None") is None
        assert parse_float("---") is None
        assert parse_float("") is None
        assert parse_float(None) is None
        assert parse_float("abc") is None
        assert parse_float(True) is None
        assert parse_float(float("nan")) is None    # pandas 缺失语义不泄漏
        assert parse_float(float("inf")) is None

    def test_parse_date(self):
        assert parse_date("20260630") == date(2026, 6, 30)
        assert parse_date("2026-06-30") == date(2026, 6, 30)
        assert parse_date("2026年06月30日") == date(2026, 6, 30)
        assert parse_date(datetime(2026, 6, 30, 12)) == date(2026, 6, 30)
        assert parse_date(date(2026, 6, 30)) == date(2026, 6, 30)
        assert parse_date("None") is None
        assert parse_date("") is None
        assert parse_date("2026-13-40") is None

    def test_parse_scale_real_string(self):
        value, as_of, unit = parse_scale("948.72亿元（截止至：2026年06月30日）")
        assert value == pytest.approx(948.72)
        assert as_of == date(2026, 6, 30)
        assert unit == "亿元"
        assert parse_scale(None) == (None, None, "")

    def test_parse_fee(self):
        assert parse_fee("0.15%（每年）") == pytest.approx(0.0015)
        assert parse_fee("---（每年）") is None
        assert parse_fee(None) is None

    def test_parse_inception(self):
        assert parse_inception("2012年05月04日 / 329.686亿份") == date(2012, 5, 4)

    def test_get_cached_ttl(self):
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            return object()

        cache = {"fetched": 0.0, "frame": None}
        first = _get_cached(cache, fetcher)
        second = _get_cached(cache, fetcher)
        assert first is second
        assert calls["n"] == 1


def _income_frame():
    return pd.DataFrame([
        {"报告日": "20250930", "公告日期": "20251025",
         "营业总收入": "3000000000.0", "归属于母公司所有者的净利润": "330000000.0",
         "营业成本": "None"},
        {"报告日": "20260331", "公告日期": "20260425",
         "营业总收入": "1000000000.0", "归属于母公司所有者的净利润": "100000000.0"},
        {"报告日": "20260630", "公告日期": "20260815",
         "营业总收入": "2300000000.0", "归属于母公司所有者的净利润": "260000000.0"},
    ])


def _balance_frame():
    return pd.DataFrame([
        {"报告日": "20260630", "资产总计": "5000000000", "负债合计": "2000000000",
         "归属于母公司股东权益合计": "3000000000", "货币资金": "800000000",
         "短期借款": "200000000", "一年内到期的非流动负债": "100000000",
         "应收账款": "150000000"},
    ])


def _cashflow_frame():
    return pd.DataFrame([
        {"报告日": "20260630", "经营活动产生的现金流量净额": "280000000"},
    ])


def _indicator_frame():
    return pd.DataFrame([
        {"日期": "2026-06-30", "扣除非经常性损益后的净利润(元)": "250000000"},
    ])


def _valuation_frame():
    return pd.DataFrame([
        {"数据日期": "2026-09-25", "当日收盘价": 100.0, "PE(TTM)": 10.0, "市净率": 1.0},
        {"数据日期": "2026-09-26", "当日收盘价": 105.0, "PE(TTM)": 12.0, "市净率": 1.2},
        {"数据日期": "2026-09-29", "当日收盘价": 110.0, "PE(TTM)": 15.0, "市净率": 1.5},
    ])


class TestBuildStockExtract:
    def _extract(self, **kwargs):
        reports = {
            "利润表": _income_frame(),
            "资产负债表": _balance_frame(),
            "现金流量表": _cashflow_frame(),
        }
        reports.update(kwargs.pop("reports", {}))
        return build_stock_extract(
            "600519", reports,
            indicator_df=kwargs.pop("indicator_df", _indicator_frame()),
            valuation_df=kwargs.pop("valuation_df", _valuation_frame()),
            pe_window=2, pe_min_samples=2,
        )

    def test_merge_by_period(self):
        extract = self._extract()
        periods = extract["periods"]
        assert [str(p["period"]) for p in periods] == [
            "2025-09-30", "2026-03-31", "2026-06-30",
        ]
        latest = periods[-1]
        assert latest["revenue"] == pytest.approx(2.3e9)
        assert latest["net_profit"] == pytest.approx(2.6e8)
        assert latest["total_assets"] == pytest.approx(5e9)
        assert latest["total_liabilities"] == pytest.approx(2e9)
        assert latest["equity_parent"] == pytest.approx(3e9)
        assert latest["cash"] == pytest.approx(8e8)
        assert latest["short_debt"] == pytest.approx(3e8)   # 2e8 + 1e8
        assert latest["ocf"] == pytest.approx(2.8e8)
        assert latest["receivables"] == pytest.approx(1.5e8)
        # 指标表合并: 扣非
        assert latest["deducted_net_profit"] == pytest.approx(2.5e8)
        # 公告日期
        assert latest["announced"] == date(2026, 8, 15)

    def test_not_financial_for_regular_income_frame(self):
        assert self._extract()["is_financial"] is False

    def test_financial_detected_by_income_structure(self):
        bank_income = pd.DataFrame([
            {"报告日": "20260630", "营业收入": "5e11", "净利息收入": "3e11",
             "归属于母公司所有者的净利润": "1.5e11"},
        ])
        extract = self._extract(reports={"利润表": bank_income})
        assert extract["is_financial"] is True

    def test_bank_equity_field_name(self):
        # 银行资产负债表用 "归属于母公司股东的权益" (名称含"的"), 需与工商企业口径互为回退
        bank_balance = pd.DataFrame([
            {"报告日": "20260630", "资产总计": "5.7e13", "负债合计": "5.2e13",
             "归属于母公司股东的权益": "4.3e12"},
        ])
        extract = self._extract(reports={"资产负债表": bank_balance})
        assert extract["periods"][-1]["equity_parent"] == pytest.approx(4.3e12)

    def test_valuation_percentile(self):
        extract = self._extract()
        valuation = extract["valuation"]
        assert valuation["date"] == date(2026, 9, 29)
        assert valuation["pe_ttm"] == pytest.approx(15.0)
        assert valuation["pe_percentile"] == pytest.approx(1.0)  # 窗口 [12,15] 中 15 最大
        assert valuation["pb_percentile"] == pytest.approx(1.0)

    def test_valuation_percentile_needs_samples(self):
        extract = build_stock_extract(
            "600519",
            {"利润表": _income_frame(), "资产负债表": None, "现金流量表": None},
            valuation_df=_valuation_frame(), pe_window=5, pe_min_samples=5,
        )
        assert extract["valuation"]["pe_percentile"] is None

    def test_missing_reports_yields_empty_periods(self):
        extract = build_stock_extract(
            "600519", {"利润表": None, "资产负债表": None, "现金流量表": None},
        )
        assert extract["periods"] == []
        assert extract["valuation"] is None


class TestEtfParsers:
    def _snapshot(self, nav: str = "4.4133") -> pd.DataFrame:
        return pd.DataFrame([
            {"基金代码": "510300", "基金简称": "沪深300ETF",
             "2026-09-29-单位净值": nav, "2026-09-29-累计净值": "4.5",
             "2026-09-24-单位净值": "4.5000", "2026-09-24-累计净值": "4.6",
             "增长值": "x", "增长率": "x", "市价": "4.4000", "折价率": "0.30%"},
            {"基金代码": "513100", "基金简称": "纳指ETF",
             "2026-09-29-单位净值": "---", "2026-09-29-累计净值": "---",
             "2026-09-24-单位净值": "2.0498", "2026-09-24-累计净值": "10.249",
             "市价": "2.3040", "折价率": "---"},
        ])

    def test_premium_ok(self):
        result = parse_premium_from_snapshot(self._snapshot(), "510300")
        assert result["status"] == "ok"
        assert result["nav"] == pytest.approx(4.4133)
        assert result["nav_date"] == date(2026, 9, 29)
        expected = (4.4133 - 4.40) / 4.4133
        assert result["discount_pct"] == pytest.approx(expected, rel=1e-6)

    def test_premium_unknown_when_nav_missing(self):
        result = parse_premium_from_snapshot(self._snapshot(), "513100")
        assert result["status"] == "unknown"
        assert result["nav"] is None          # 当日 '---' → None (不拿旧净值冒充当日)
        assert "不解除" in result["note"]
        assert "不解除" in result["note"]

    def test_premium_unknown_when_code_not_covered(self):
        result = parse_premium_from_snapshot(self._snapshot(), "999999")
        assert result["status"] == "unknown"

    def test_overview_parse(self):
        overview = pd.DataFrame([{
            "基金全称": "华泰柏瑞沪深300ETF", "基金类型": "指数型-股票",
            "成立日期/规模": "2012年05月04日 / 329.686亿份",
            "净资产规模": "948.72亿元（截止至：2026年06月30日）",
            "跟踪标的": "沪深300指数", "业绩比较基准": "沪深300指数",
            "基金管理人": "华泰柏瑞基金", "基金托管人": "工商银行",
            "管理费率": "0.15%（每年）", "托管费率": "0.05%（每年）",
        }])
        profile = parse_overview_frame(overview, "510300")
        assert profile["track_index"] == "沪深300指数"
        assert profile["inception"] == date(2012, 5, 4)
        assert profile["scale"]["value"] == pytest.approx(948.72)
        assert profile["scale"]["as_of"] == date(2026, 6, 30)
        assert profile["management_fee"] == pytest.approx(0.0015)
        assert profile["custody_fee"] == pytest.approx(0.0005)

    def test_overview_empty_raises(self):
        with pytest.raises(ValueError):
            parse_overview_frame(pd.DataFrame(), "510300")

    def test_spread_parse(self):
        category = pd.DataFrame([
            {"代码": "sh510300", "名称": "沪深300ETF", "买入": "4.410", "卖出": "4.420"},
            {"代码": "sz159915", "名称": "创业板ETF", "买入": "---", "卖出": "2.0"},
        ])
        result = parse_spread_from_category(category, "510300")
        assert result["bid"] == pytest.approx(4.41)
        assert result["spread_pct"] == pytest.approx(0.01 / 4.41, rel=1e-6)
        missing = parse_spread_from_category(category, "159915")
        assert missing["spread_pct"] is None
        unknown = parse_spread_from_category(category, "999999")
        assert unknown["spread_pct"] is None
