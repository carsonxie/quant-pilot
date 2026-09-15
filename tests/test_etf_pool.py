"""ETF 四层池 (M3) 单元测试.

覆盖: 分项检查 (pass/fail/unknown/not_applicable)、can_buy/can_sell/can_hold、
NAV 缺失对必需产品 (跨境) 的禁买、停牌 (stale) 语义、持仓独立于筛选、
产品契约同步与池版本 (幂等/出池关闭)、历史池判定 (当时未上市 / 条件性研究).
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from data_pipeline.duckdb_manager import DuckDBManager
from strategies import etf_pool


def _days(end: date, n: int) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


END = date(2026, 9, 11)


def make_ohlcv(
    spec: dict[str, dict],
    n_days: int = 80,
    end: date = END,
) -> pl.DataFrame:
    """spec: symbol → {market_type, amount?, drop_last?} (drop_last=末几日缺失)."""
    rows = []
    for symbol, cfg in spec.items():
        dates = _days(end, n_days)
        drop = int(cfg.get("drop_last", 0))
        if drop:
            dates = dates[: -drop]
        for i, d in enumerate(dates):
            price = 3.0 * (1.0005 ** i)
            rows.append({
                "symbol": symbol, "date": d,
                "open": price * 0.999, "high": price * 1.002,
                "low": price * 0.998, "close": price,
                "volume": 1_000_000.0,
                "amount": cfg.get("amount", 8_000_000.0),
            })
    return pl.DataFrame(rows)


def make_metadata(spec: dict[str, dict]) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol, "name": symbol,
            "market_type": cfg["market_type"], "settlement_type": "T+1",
        }
        for symbol, cfg in spec.items()
    ])


def make_settings() -> dict:
    return {
        "universe_filter": {
            "min_avg_volume": 1_000_000, "min_list_days": 60,
            "max_premium_pct": 3.0, "liquidity_concentration_warn": 0.005,
            "premium_check_required_market_types": ["跨境"],
        },
    }


SPEC = {
    "510300": {"market_type": "A股"},
    "513100": {"market_type": "跨境"},
    "518880": {"market_type": "商品"},
}


class TestTradableUniverse:
    def _report(self, spec: dict | None = None, nav_df: pl.DataFrame | None = None):
        spec = spec or SPEC
        return etf_pool.build_pool_report(
            make_ohlcv(spec), metadata_df=make_metadata(spec), nav_df=nav_df,
            decision_date=END, settings=make_settings(),
        )

    def test_nav_missing_blocks_required_type_only(self):
        report = self._report()
        entries = report.by_symbol
        # 跨境: 溢价检查必需 + NAV 缺失 → 禁新买, 但可持有/估值
        assert entries["513100"].checks["premium"] == "unknown"
        assert entries["513100"].can_buy is False
        assert entries["513100"].can_hold is True
        assert any("禁新买" in r for r in entries["513100"].reasons)
        # A股/商品: 溢价为非必需维度 → not_applicable, 不影响买入
        assert entries["510300"].checks["premium"] == "not_applicable"
        assert entries["510300"].can_buy is True
        assert entries["518880"].can_buy is True
        assert report.buyable == ["510300", "518880"]
        assert "513100" in report.unknown

    def test_nav_available_premium_pass_enables_buy(self):
        nav = pl.DataFrame({
            "symbol": ["513100"], "date": [END], "nav": [3.10],
        })
        report = self._report(nav_df=nav)
        entry = report.by_symbol["513100"]
        assert entry.checks["premium"] == "pass"
        assert entry.can_buy is True

    def test_nav_available_premium_fail_blocks_buy(self):
        nav = pl.DataFrame({
            "symbol": ["513100"], "date": [END], "nav": [1.00],
        })
        report = self._report(nav_df=nav)
        entry = report.by_symbol["513100"]
        assert entry.checks["premium"] == "fail"
        assert entry.can_buy is False
        assert entry.can_hold is True

    def test_stale_symbol_cannot_buy_or_sell(self):
        spec = {
            "510300": {"market_type": "A股"},
            "510050": {"market_type": "A股", "drop_last": 2},  # 停牌/缺数
        }
        report = self._report(spec)
        entry = report.by_symbol["510050"]
        assert entry.stale is True
        assert entry.checks["data_freshness"] == "fail"
        assert entry.can_buy is False
        assert entry.can_sell is False   # 无有效价格不能卖
        assert entry.can_hold is True    # 持仓保留并按最近价估值
        assert report.stale_symbols == {"510050"}
        assert report.by_symbol["510300"].can_buy is True

    def test_volume_fail_blocks_buy_but_allows_sell_hold(self):
        spec = {"510300": {"market_type": "A股", "amount": 100_000.0}}
        report = self._report(spec)
        entry = report.by_symbol["510300"]
        assert entry.checks["volume"] == "fail"
        assert entry.can_buy is False
        assert entry.can_sell is True
        assert entry.can_hold is True

    def test_holdings_do_not_change_filters(self):
        report = etf_pool.build_pool_report(
            make_ohlcv(SPEC), metadata_df=make_metadata(SPEC), nav_df=None,
            decision_date=END, settings=make_settings(),
            holdings={"513100": 1000.0},
        )
        entry = report.by_symbol["513100"]
        assert entry.is_held is True
        assert entry.can_hold is True
        assert entry.can_buy is False  # 禁买不因持仓解除

    def test_membership_mode_conditional_before_version(self):
        report = etf_pool.build_pool_report(
            make_ohlcv(SPEC), metadata_df=make_metadata(SPEC), nav_df=None,
            decision_date=END, settings=make_settings(),
            latest_version={"version_id": "uv-x", "as_of": date(2026, 9, 20)},
        )
        assert report.membership_mode == "conditional_research"
        assert any("条件性研究" in note for note in report.notes)


class TestHistoricalMembership:
    def test_not_buyable_before_listing(self):
        ok, why = etf_pool.member_buyable_at(
            "588000", date(2020, 1, 2),
            list_date=date(2020, 11, 16), version_as_of=date(2026, 9, 14),
        )
        assert ok is False
        assert why == "当时未上市"

    def test_conditional_before_version_declared(self):
        ok, why = etf_pool.member_buyable_at(
            "510300", date(2021, 1, 4),
            list_date=date(2012, 5, 28), version_as_of=date(2026, 9, 14),
        )
        assert ok is True
        assert "conditional_research" in why

    def test_declared_after_version(self):
        ok, why = etf_pool.member_buyable_at(
            "510300", date(2026, 9, 14),
            list_date=date(2012, 5, 28), version_as_of=date(2026, 9, 14),
        )
        assert ok is True
        assert why == "declared"


def _pool_settings(symbols: list[str]) -> dict:
    return {
        "universe": {
            "broad_base": [
                {
                    "symbol": s, "name": s, "market_type": "A股",
                    "settlement": "T+1", "index_name": None,
                }
                for s in symbols
            ],
        },
        "universe_filter": {"premium_check_required_market_types": ["跨境"]},
        "data_quality": {
            "daily_change": {
                "default_limit_pct": 10.0,
                "limit_overrides_pct": {"159915": 20.0},
            },
        },
    }


def _basic_ohlcv(symbols: list[str]) -> pl.DataFrame:
    rows = []
    for symbol in symbols:
        for d in (date(2026, 9, 10), date(2026, 9, 11)):
            rows.append({
                "symbol": symbol, "date": d, "open": 3.0, "high": 3.0,
                "low": 3.0, "close": 3.0, "volume": 1e6, "amount": 8e6,
            })
    return pl.DataFrame(rows)


class TestSyncPool:
    def test_sync_creates_and_is_idempotent(self, tmp_path):
        db = DuckDBManager(str(tmp_path / "pool.duckdb"))
        db.init_database()
        settings = _pool_settings(["510300"])
        db.upsert_ohlcv(_basic_ohlcv(["510300"]))

        v1 = etf_pool.sync_instruments_and_pool(
            db, settings, ["510300"], as_of=date(2026, 9, 14),
        )
        v2 = etf_pool.sync_instruments_and_pool(
            db, settings, ["510300"], as_of=date(2026, 9, 14),
        )
        assert v1 == v2
        assert db.get_table_count("universe_versions") == 1

        members = db.get_universe_members(v1)
        assert members["symbol"].to_list() == ["510300"]
        assert members["effective_to"][0] is None

        instruments = db.get_instruments(["510300"])
        row = instruments.row(0, named=True)
        assert row["exchange"] == "SH"
        assert row["price_limit_pct"] == 10.0
        assert row["list_date"] == date(2026, 9, 10)  # 派生数据起点
        assert row["premium_check_required"] is False
        db.close()

    def test_pool_change_creates_new_version_and_closes_member(self, tmp_path):
        db = DuckDBManager(str(tmp_path / "pool2.duckdb"))
        db.init_database()
        settings_v1 = _pool_settings(["510300", "510050"])
        db.upsert_ohlcv(_basic_ohlcv(["510300", "510050"]))
        v1 = etf_pool.sync_instruments_and_pool(
            db, settings_v1, ["510300", "510050"], as_of=date(2026, 9, 14),
        )

        settings_v2 = _pool_settings(["510300"])
        v2 = etf_pool.sync_instruments_and_pool(
            db, settings_v2, ["510300"], as_of=date(2026, 9, 15),
        )
        assert v2 != v1
        # 出池成员: 区间终点被标注, 历史保留
        open_symbols = set(db.get_open_universe_members()["symbol"].to_list())
        assert "510050" not in open_symbols
        history = db.get_universe_members(v1)
        assert set(history["symbol"].to_list()) == {"510300", "510050"}
        closed = history.filter(pl.col("symbol") == "510050").row(0, named=True)
        assert closed["effective_to"] == date(2026, 9, 14)
        db.close()

    def test_upsert_nav_and_pool_report_uses_it(self, tmp_path):
        db = DuckDBManager(str(tmp_path / "pool3.duckdb"))
        db.init_database()
        db.upsert_nav_daily(pl.DataFrame([{
            "symbol": "513100", "date": END, "nav": 3.10, "source": "test",
        }]))
        nav_df = db.get_nav_daily(["513100"])
        assert nav_df.height == 1
        # 幂等更新
        db.upsert_nav_daily(pl.DataFrame([{
            "symbol": "513100", "date": END, "nav": 3.11, "source": "test",
        }]))
        assert db.get_nav_daily(["513100"])["nav"][0] == pytest.approx(3.11)
        db.close()
