"""条件检查规则层测试 (P11-M4; 手工期望值, cond-v0.1-research)."""

from datetime import date, timedelta

import polars as pl
import pytest

from strategies.condition_checks import (
    STATUS_NA,
    STATUS_OK,
    STATUS_REVIEW,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    evaluate_etf_conditions,
    evaluate_stock_conditions,
    load_condition_rules,
    summarize_items,
)

_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


def _period(year: int, month: int, **values) -> dict:
    base = {
        "period": date(year, month, _DAY[month]), "announced": None,
        "revenue": None, "net_profit": None, "deducted_net_profit": None,
        "ocf": None, "total_assets": None, "total_liabilities": None,
        "equity_parent": None, "cash": None, "short_debt": None, "receivables": None,
    }
    base.update(values)
    return base


def _stock_extract(periods, *, valuation=None, is_financial=False) -> dict:
    return {
        "symbol": "600519", "is_financial": is_financial, "periods": periods,
        "valuation": valuation, "errors": [],
    }


def _by_id(items, item_id):
    return next(item for item in items if item.item_id == item_id)


@pytest.fixture(scope="module")
def rules():
    return load_condition_rules()


class TestStockProfitability:
    def _two_periods(self, **latest):
        return [
            _period(2025, 6, revenue=100.0, net_profit=50.0),
            _period(2026, 6, **latest),
        ]

    def test_revenue_yoy_ok_watch_review(self, rules):
        ok = evaluate_stock_conditions(
            _stock_extract(self._two_periods(revenue=120.0, net_profit=60.0)), rules=rules,
        )
        assert _by_id(ok, "PL-01").status == STATUS_OK
        assert "+20.0%" in _by_id(ok, "PL-01").detail

        watch = evaluate_stock_conditions(
            _stock_extract(self._two_periods(revenue=85.0, net_profit=60.0)), rules=rules,
        )
        assert _by_id(watch, "PL-01").status == STATUS_WATCH

        review = evaluate_stock_conditions(
            _stock_extract(self._two_periods(revenue=60.0, net_profit=60.0)), rules=rules,
        )
        assert _by_id(review, "PL-01").status == STATUS_REVIEW

    def test_negative_base_no_misleading_growth(self, rules):
        periods = [
            _period(2025, 6, revenue=100.0, net_profit=-50.0),
            _period(2026, 6, revenue=120.0, net_profit=30.0),
        ]
        items = evaluate_stock_conditions(_stock_extract(periods), rules=rules)
        net = _by_id(items, "PL-02")
        assert net.status == STATUS_UNKNOWN
        assert "基期为负" in net.detail

    def test_negative_profit_is_review(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._two_periods(revenue=100.0, net_profit=-20.0)), rules=rules,
        )
        assert _by_id(items, "PL-02").status == STATUS_REVIEW

    def test_deducted_negative_with_positive_net_is_watch(self, rules):
        # 归母 48 vs 基期 50 → 同比 -4% (未触发); 扣非为负 → 提升为关注
        items = evaluate_stock_conditions(
            _stock_extract(self._two_periods(
                revenue=102.0, net_profit=48.0, deducted_net_profit=-5.0,
            )),
            rules=rules,
        )
        net = _by_id(items, "PL-02")
        assert net.status == STATUS_WATCH
        assert "扣非" in net.detail

    def test_ttm_from_quarters(self, rules):
        periods = [
            _period(2025, 3, revenue=1e9, net_profit=1e9),
            _period(2025, 6, revenue=2.5e9, net_profit=2.5e9),
            _period(2025, 9, revenue=4.5e9, net_profit=4.5e9),
            _period(2025, 12, revenue=7e9, net_profit=7e9),
            _period(2026, 3, revenue=2e9, net_profit=2e9),
            _period(2026, 6, revenue=4.5e9, net_profit=4.5e9),
        ]
        items = evaluate_stock_conditions(_stock_extract(periods), rules=rules)
        ttm = _by_id(items, "PL-03")
        assert ttm.status == STATUS_OK
        # 单季: 10/15/20/25/20/25 → 最近四季 = 20+25+20+25 = 90
        assert "90.00 亿" in ttm.detail

    def test_ttm_not_fabricated_when_incomplete(self, rules):
        periods = [
            _period(2026, 3, net_profit=10.0),
            _period(2026, 6, net_profit=25.0),
        ]
        items = evaluate_stock_conditions(_stock_extract(periods), rules=rules)
        assert _by_id(items, "PL-03").status == STATUS_UNKNOWN

    def test_no_periods_yields_single_unknown(self, rules):
        items = evaluate_stock_conditions(_stock_extract([]), rules=rules)
        assert len(items) == 1
        assert items[0].status == STATUS_UNKNOWN


class TestStockCashflowAndSolvency:
    def _periods(self, **latest):
        merged = {
            "total_assets": 1000.0, "total_liabilities": 400.0,
            "equity_parent": 600.0, "cash": 200.0, "short_debt": 100.0,
        }
        merged.update(latest)
        return [
            _period(2025, 6, revenue=100.0, net_profit=50.0, ocf=50.0),
            _period(2026, 6, revenue=120.0, net_profit=60.0, **merged),
        ]

    def test_ocf_negative_is_watch(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._periods(ocf=-30.0)), rules=rules,
        )
        assert _by_id(items, "CF-01").status == STATUS_WATCH

    def test_ocf_to_profit_low_is_watch(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._periods(ocf=18.0)), rules=rules,  # 18/60 = 0.3 < 0.5
        )
        assert _by_id(items, "CF-01").status == STATUS_WATCH

    def test_ocf_healthy(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._periods(ocf=50.0)), rules=rules,
        )
        assert _by_id(items, "CF-01").status == STATUS_OK

    def test_debt_ratio_levels(self, rules):
        watch = evaluate_stock_conditions(
            _stock_extract(self._periods(total_liabilities=750.0)), rules=rules,
        )
        assert _by_id(watch, "SV-01").status == STATUS_WATCH
        review = evaluate_stock_conditions(
            _stock_extract(self._periods(total_liabilities=900.0)), rules=rules,
        )
        assert _by_id(review, "SV-01").status == STATUS_REVIEW

    def test_short_debt_cover(self, rules):
        review = evaluate_stock_conditions(
            _stock_extract(self._periods(cash=40.0, short_debt=100.0)), rules=rules,
        )
        assert _by_id(review, "SV-02").status == STATUS_REVIEW
        ok = evaluate_stock_conditions(
            _stock_extract(self._periods(cash=150.0, short_debt=100.0)), rules=rules,
        )
        assert _by_id(ok, "SV-02").status == STATUS_OK
        missing = evaluate_stock_conditions(
            _stock_extract(self._periods(short_debt=None)), rules=rules,
        )
        assert _by_id(missing, "SV-02").status == STATUS_UNKNOWN

    def test_negative_equity_review(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._periods(equity_parent=-10.0)), rules=rules,
        )
        assert _by_id(items, "SV-03").status == STATUS_REVIEW

    def test_financial_company_na(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract(self._periods(), is_financial=True), rules=rules,
        )
        assert _by_id(items, "SV-01").status == STATUS_NA
        assert "金融企业" in _by_id(items, "SV-01").detail
        assert _by_id(items, "SV-02").status == STATUS_NA


class TestStockValuation:
    def _extract(self, valuation):
        return _stock_extract(
            [_period(2026, 6, revenue=100.0, net_profit=50.0)],
            valuation=valuation,
        )

    def test_missing_valuation_unknown(self, rules):
        items = evaluate_stock_conditions(self._extract(None), rules=rules)
        assert _by_id(items, "VAL-01").status == STATUS_UNKNOWN
        assert _by_id(items, "VAL-02").status == STATUS_UNKNOWN

    def test_negative_pe_not_cheap(self, rules):
        items = evaluate_stock_conditions(
            self._extract({"pe_ttm": -12.0, "pb": 1.2, "pe_percentile": None,
                           "pb_percentile": 0.5, "date": date(2026, 9, 29)}),
            rules=rules,
        )
        pe = _by_id(items, "VAL-01")
        assert pe.status == STATUS_UNKNOWN
        assert "不解释为便宜" in pe.detail

    def test_high_percentile_watch(self, rules):
        items = evaluate_stock_conditions(
            self._extract({"pe_ttm": 30.0, "pb": 3.0, "pe_percentile": 0.95,
                           "pb_percentile": 0.4, "date": date(2026, 9, 29)}),
            rules=rules,
        )
        assert _by_id(items, "VAL-01").status == STATUS_WATCH
        assert _by_id(items, "VAL-02").status == STATUS_OK

    def test_percentile_sample_short(self, rules):
        items = evaluate_stock_conditions(
            self._extract({"pe_ttm": 20.0, "pb": 2.0, "pe_percentile": None,
                           "pb_percentile": None, "date": date(2026, 9, 29)}),
            rules=rules,
        )
        assert _by_id(items, "VAL-01").status == STATUS_OK
        assert "样本不足" in _by_id(items, "VAL-01").detail


def _etf_extract(*, profile=None, premium=None, spread=None) -> dict:
    return {
        "symbol": "510300", "profile": profile, "premium": premium or {},
        "spread": spread or {"bid": None, "ask": None, "spread_pct": None, "note": "缺"},
        "errors": [],
    }


def _profile(**values) -> dict:
    base = {
        "full_name": "测试ETF", "fund_type": "指数型-股票", "inception": date(2012, 5, 4),
        "track_index": "沪深300指数", "benchmark": "", "manager": "管理人", "custodian": "托管人",
        "scale": {"value": 100.0, "unit": "亿元", "as_of": date(2026, 6, 30)},
        "management_fee": 0.0015, "custody_fee": 0.0005,
    }
    base.update(values)
    return base


def _prices(amount: float, n: int = 25) -> pl.DataFrame:
    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({
        "date": days, "open": [1.0] * n, "high": [1.1] * n, "low": [0.9] * n,
        "close": [1.0] * n, "volume": [1e6] * n, "amount": [amount] * n,
    })


class TestEtfConditions:
    def _run(self, extract, rules, *, amount=5e7, market_type="A股"):
        return evaluate_etf_conditions(
            extract, prices=_prices(amount), last_completed=date(2026, 9, 29),
            market_type=market_type, rules=rules,
        )

    def test_profile_missing_unknown(self, rules):
        items = self._run(_etf_extract(profile=None), rules)
        assert _by_id(items, "EP-01").status == STATUS_UNKNOWN
        assert _by_id(items, "EP-02").status == STATUS_UNKNOWN
        assert _by_id(items, "EP-03").status == STATUS_UNKNOWN

    def test_scale_levels_and_staleness(self, rules):
        review = self._run(_etf_extract(profile=_profile(
            scale={"value": 1.5, "unit": "亿元", "as_of": date(2026, 6, 30)},
        )), rules)
        assert _by_id(review, "EP-02").status == STATUS_REVIEW
        watch = self._run(_etf_extract(profile=_profile(
            scale={"value": 4.0, "unit": "亿元", "as_of": date(2026, 6, 30)},
        )), rules)
        assert _by_id(watch, "EP-02").status == STATUS_WATCH
        stale = self._run(_etf_extract(profile=_profile(
            scale={"value": 50.0, "unit": "亿元", "as_of": date(2025, 6, 30)},
        )), rules)
        assert "未更新" in _by_id(stale, "EP-02").detail

    def test_fee_watch(self, rules):
        items = self._run(_etf_extract(profile=_profile(
            management_fee=0.008, custody_fee=0.001,
        )), rules)
        assert _by_id(items, "EP-03").status == STATUS_WATCH

    def test_liquidity_thresholds(self, rules):
        watch = self._run(_etf_extract(profile=_profile()), rules, amount=1e7)
        assert _by_id(watch, "LIQ-01").status == STATUS_WATCH
        review = self._run(_etf_extract(profile=_profile()), rules, amount=3e6)
        assert _by_id(review, "LIQ-01").status == STATUS_REVIEW
        scarce = evaluate_etf_conditions(
            _etf_extract(profile=_profile()), prices=_prices(5e7, n=3),
            last_completed=date(2026, 9, 29), market_type="A股", rules=rules,
        )
        assert _by_id(scarce, "LIQ-01").status == STATUS_UNKNOWN

    def test_spread_and_premium(self, rules):
        items = self._run(_etf_extract(
            profile=_profile(),
            spread={"bid": 4.41, "ask": 4.45, "spread_pct": 0.009, "note": ""},
            premium={"status": "ok", "nav": 4.0, "nav_date": date(2026, 9, 29),
                     "price": 4.3, "discount_pct": -0.075},
        ), rules)
        assert _by_id(items, "LIQ-02").status == STATUS_WATCH
        pr = _by_id(items, "PR-01")
        assert pr.status == STATUS_WATCH          # |折溢价| 7.5% > 3%
        assert "折价" in pr.detail

    def test_premium_unknown_keeps_note(self, rules):
        note = "当日净值缺失/未更新 或 市价缺失 → 折溢价无法评估 (不解除既有禁买)"
        items = self._run(_etf_extract(
            profile=_profile(),
            premium={"status": "unknown", "note": note},
        ), rules)
        pr = _by_id(items, "PR-01")
        assert pr.status == STATUS_UNKNOWN
        assert "不解除" in pr.detail

    def test_deferred_and_not_applicable(self, rules):
        items = self._run(_etf_extract(profile=_profile()), rules)
        assert _by_id(items, "TQ-01").status == STATUS_NA
        assert "暂缓" in _by_id(items, "TQ-01").detail
        assert _by_id(items, "UV-01").status == STATUS_NA

        gold = self._run(_etf_extract(profile=_profile()), rules, market_type="商品")
        assert "不适用" in _by_id(gold, "UV-01").detail


class TestSummary:
    def test_summarize_counts(self, rules):
        items = evaluate_stock_conditions(
            _stock_extract([
                _period(2025, 6, revenue=100.0, net_profit=50.0),
                _period(2026, 6, revenue=60.0, net_profit=-10.0),
            ]),
            rules=rules,
        )
        summary = summarize_items(items)
        # 营收 -40% → 重点复核; 净利为负 → 重点复核; 其余字段缺失 → 无法评估
        assert "重点复核 2" in summary
        assert "无法评估" in summary
