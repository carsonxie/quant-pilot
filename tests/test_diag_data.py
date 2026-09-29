"""诊断数据服务测试 (P11-M2-03/04/05; 不依赖网络: 取数函数注入替身)."""

from datetime import date, datetime, timedelta
from datetime import time as dt_time

import polars as pl
import pytest

from data_pipeline.diag_data import (
    DEFAULT_MIN_HISTORY_DAYS,
    FetchError,
    TradingCalendar,
    _normalize_hist,
    _retry_call,
    adjust_prices_for_events,
    check_equity_events,
    detect_unregistered_jumps,
    ensure_research_prices,
    last_completed_trading_day,
    load_trading_calendar,
    price_limit_pct,
    reset_calendar_cache,
)
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.securities import SecurityIdentity


@pytest.fixture(autouse=True)
def _clear_calendar_cache():
    reset_calendar_cache()
    yield
    reset_calendar_cache()


@pytest.fixture
def db(tmp_path):
    db = DuckDBManager(str(tmp_path / "diag.duckdb"))
    db.init_database()
    yield db
    db.close()


def _weekdays(start: date, n: int) -> list[date]:
    days, cur = [], start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _make_prices(symbol: str, days: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol, "date": d, "open": c, "high": c * 1.01,
            "low": c * 0.99, "close": c, "volume": 1e6, "amount": 1e7,
        }
        for d, c in zip(days, closes, strict=True)
    ])


STOCK = SecurityIdentity(
    symbol="600519", name="贵州茅台", exchange="SH", sec_type="stock",
    market_type="A股", adjustment="hfq", source="test",
)
ETF = SecurityIdentity(
    symbol="512000", name="券商ETF", exchange="SH", sec_type="etf",
    market_type="A股", adjustment="none", source="test",
)
POOL_ETF = SecurityIdentity(
    symbol="510300", name="沪深300ETF", exchange="SH", sec_type="etf",
    market_type="A股", adjustment="none", source="test",
)


class CountingFetcher:
    def __init__(self, factory):
        self.factory = factory
        self.calls = 0

    def __call__(self, symbol):
        self.calls += 1
        return self.factory(symbol)


class TestTradingCalendar:
    def _cal(self):
        days = _weekdays(date(2026, 8, 3), 15)
        return TradingCalendar(days=tuple(days), source="sina"), days

    def test_before_cutoff_uses_previous_day(self):
        cal, days = self._cal()
        now = datetime.combine(days[5], dt_time(15, 0))  # 交易日 15:00 (< 18:30)
        assert last_completed_trading_day(cal, now=now) == days[4]

    def test_after_cutoff_uses_today(self):
        cal, days = self._cal()
        now = datetime.combine(days[5], dt_time(19, 0))
        assert last_completed_trading_day(cal, now=now) == days[5]

    def test_weekend_uses_friday(self):
        cal, days = self._cal()
        saturday = days[4] + timedelta(days=1)  # 周六
        now = datetime.combine(saturday, dt_time(12, 0))
        assert last_completed_trading_day(cal, now=now) == days[4]

    def test_holiday_not_in_calendar_is_not_delay(self):
        # 周三 10:00, 但周三缺档 (假想休市) → 取周一
        monday, wednesday = date(2026, 8, 3), date(2026, 8, 5)
        cal = TradingCalendar(days=(monday,), source="sina")
        now = datetime.combine(wednesday, dt_time(10, 0))
        assert last_completed_trading_day(cal, now=now) == monday

    def test_empty_calendar_returns_none(self):
        cal = TradingCalendar(days=(), source="sina")
        assert last_completed_trading_day(cal) is None

    def test_load_with_fetcher_and_cache(self):
        days = _weekdays(date(2026, 8, 3), 5)
        calls = {"n": 0}

        def cal_fetcher() -> pl.DataFrame:
            calls["n"] += 1
            return pl.DataFrame({"trade_date": days})

        cal = load_trading_calendar(fetcher=cal_fetcher)
        assert cal.source == "sina"
        assert list(cal.days) == days
        load_trading_calendar(fetcher=cal_fetcher)  # 命中进程缓存
        assert calls["n"] == 1

    def test_load_fallback_to_pool_dates(self, db):
        pool_days = _weekdays(date(2026, 8, 3), 4)
        db.upsert_ohlcv(_make_prices("510300", pool_days, [4.0] * 4))

        def _boom():
            raise RuntimeError("network down")

        cal = load_trading_calendar(db=db, fetcher=_boom)
        assert cal.source == "pool_fallback"
        assert list(cal.days) == pool_days

    def test_load_raises_without_fallback(self):
        def _boom():
            raise RuntimeError("network down")

        with pytest.raises(FetchError):
            load_trading_calendar(fetcher=_boom)


class TestNormalize:
    def test_normalize_string_and_extra_columns(self):
        raw = pl.DataFrame({
            "date": ["2024-01-15", "2024-01-16"],
            "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5],
            "volume": [100, 200], "prevclose": [9, 9],
        })
        out = _normalize_hist(raw, "510300")
        assert out.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
        assert out["date"].to_list() == [date(2024, 1, 15), date(2024, 1, 16)]
        assert out["amount"].to_list() == [None, None]
        assert out["symbol"].to_list() == ["510300", "510300"]

    def test_normalize_filters_range(self):
        days = _weekdays(date(2026, 8, 3), 6)
        raw = _make_prices("600519", days, [10.0] * 6)
        out = _normalize_hist(raw, "600519", start=days[1], end=days[3])
        assert out["date"].to_list() == days[1:4]


class TestRetry:
    def test_retries_then_succeeds(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("transient")
            return "ok"

        assert _retry_call(flaky, desc="t", retries=3, backoff=0.01, timeout=5) == "ok"
        assert attempts["n"] == 2

    def test_key_error_is_not_retried(self):
        attempts = {"n": 0}

        def bad():
            attempts["n"] += 1
            raise KeyError("date")

        with pytest.raises(FetchError):
            _retry_call(bad, desc="t", retries=3, backoff=0.01, timeout=5)
        assert attempts["n"] == 1  # 已知"代码不可查询"失败不重试

    def test_timeout_becomes_fetch_error(self):
        import time as _t

        def slow():
            _t.sleep(0.4)
            return "late"

        with pytest.raises(FetchError):
            _retry_call(slow, desc="t", retries=1, backoff=0.01, timeout=0.05)


class TestEnsureResearchPrices:
    def _cal_and_now(self, last_day_index: int = -1):
        days = _weekdays(date(2026, 8, 3), 25)
        cal = TradingCalendar(days=tuple(days), source="sina")
        now = datetime.combine(days[last_day_index], dt_time(20, 0))
        return cal, days, now

    def test_stock_first_fetch_writes_and_notes_short_history(self, db):
        cal, days, now = self._cal_and_now()
        fake_days = days[:10]
        fake = CountingFetcher(
            lambda symbol: _make_prices(symbol, fake_days, [10.0 + i for i in range(10)])
        )
        result = ensure_research_prices(
            db, STOCK, refresh="auto", settings={}, now=now, calendar=cal,
            fetchers={"stock": fake},
        )
        assert fake.calls == 1
        assert result.status == "updated"
        assert result.history_days == 10
        assert result.short_history
        assert any("历史仅" in n for n in result.notes)
        assert db.get_table_count("diag_price_daily") == 10
        # 口径版本 = 源|复权 (日期由 as_of 承载; M3-05 修正)
        assert result.price_version == "diag_price_daily|hfq"

    def test_fresh_read_only_does_not_fetch(self, db):
        cal, days, now = self._cal_and_now()
        fake_days = days[:10]
        fake = CountingFetcher(
            lambda symbol: _make_prices(symbol, fake_days, [10.0] * 10)
        )
        ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": fake},
        )
        # 数据截止 == 最近已完成交易日 (days[-1])? 否: fake 只到 days[9]
        # → 用与缓存一致的 now 模拟"数据已新鲜"
        now2 = datetime.combine(fake_days[-1], dt_time(20, 0))
        result = ensure_research_prices(
            db, STOCK, settings={}, now=now2, calendar=cal, fetchers={"stock": fake},
        )
        assert fake.calls == 1  # 不再取数
        assert result.status == "fresh"

    def test_stale_triggers_fetch(self, db):
        cal, days, now = self._cal_and_now()
        fake = CountingFetcher(
            lambda symbol: _make_prices(symbol, days, [10.0] * len(days))
        )
        # 先缓存一小段旧数据
        db.replace_diag_prices(
            "600519",
            _make_prices("600519", days[:5], [9.0] * 5).with_columns([
                pl.lit("sina_stock").alias("source"),
                pl.lit("hfq").alias("adjustment"),
            ]),
        )
        result = ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": fake},
        )
        assert result.status == "updated"
        assert fake.calls == 1
        assert result.last_date == days[-1]

    def test_refresh_none_never_fetches(self, db):
        cal, days, now = self._cal_and_now()
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days, [10.0] * len(days)))
        result = ensure_research_prices(
            db, STOCK, refresh="none", settings={}, now=now, calendar=cal,
            fetchers={"stock": fake},
        )
        assert fake.calls == 0
        assert result.status == "empty"

    def test_force_refetches_even_when_fresh(self, db):
        cal, days, now = self._cal_and_now()
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days, [10.0] * len(days)))
        ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": fake},
        )
        ensure_research_prices(
            db, STOCK, refresh="force", settings={}, now=now, calendar=cal,
            fetchers={"stock": fake},
        )
        assert fake.calls == 2

    def test_fetch_failure_keeps_cache(self, db):
        cal, days, now = self._cal_and_now()
        ok_fake = CountingFetcher(lambda symbol: _make_prices(symbol, days[:5], [10.0] * 5))
        ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": ok_fake},
        )
        before = db.get_table_count("diag_price_daily")

        def _boom(symbol):
            raise FetchError("接口超时")

        result = ensure_research_prices(
            db, STOCK, refresh="auto", settings={}, now=now, calendar=cal,
            fetchers={"stock": _boom},
        )
        assert result.status == "stale"
        assert any("刷新失败" in n for n in result.notes)
        assert db.get_table_count("diag_price_daily") == before  # 旧数据保留

    def test_fetch_failure_without_cache(self, db):
        cal, days, now = self._cal_and_now()

        def _boom(symbol):
            raise FetchError("接口超时")

        result = ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": _boom},
        )
        assert result.status == "failed"
        assert result.prices.is_empty()

    def test_quality_critical_blocks_write(self, db):
        cal, days, now = self._cal_and_now()
        closes = [10.0] * (len(days) - 1) + [16.0]  # 末日 +60% (超限阻断)
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days, closes))
        result = ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": fake},
        )
        assert result.status == "blocked"
        assert any("质检阻断" in n for n in result.notes)
        assert db.get_table_count("diag_price_daily") == 0  # 未写入

    def test_non_pool_etf_incremental_no_duplicates(self, db):
        cal, days, now = self._cal_and_now()
        # 缓存已有前 8 天
        db.upsert_diag_prices(
            _make_prices("512000", days[:8], [2.0] * 8).with_columns([
                pl.lit("sina_etf").alias("source"),
                pl.lit("none").alias("adjustment"),
            ]),
        )
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days[:10], [2.0] * 10))
        result = ensure_research_prices(
            db, ETF, settings={}, now=now, calendar=cal, fetchers={"etf": fake},
        )
        assert result.status == "updated"
        assert db.get_table_count("diag_price_daily") == 10  # 重叠行不重复
        assert result.source == "diag_price_daily"

    def test_pool_etf_reads_and_updates_ohlcv(self, db):
        cal, days, now = self._cal_and_now()
        db.upsert_ohlcv(_make_prices("510300", days[:6], [4.0] * 6))
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days, [4.0] * len(days)))
        result = ensure_research_prices(
            db, POOL_ETF, settings={}, now=now, calendar=cal,
            fetchers={"etf": fake}, pool_symbols={"510300"},
        )
        assert result.source == "ohlcv_daily"
        assert result.status == "updated"
        assert db.get_ohlcv(symbols=["510300"]).height == len(days)  # 更新进 ohlcv
        assert db.get_table_count("diag_price_daily") == 0          # 不写诊断缓存

    def test_out_of_pool_etf_with_ohlcv_history_is_pool(self, db):
        """出池/禁买持仓: ohlcv 有历史即走池内路径 (P11-A2)."""
        cal, days, now = self._cal_and_now()
        db.upsert_ohlcv(_make_prices("510500", days[:6], [6.0] * 6))
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days, [6.0] * len(days)))
        result = ensure_research_prices(
            db, SecurityIdentity(
                symbol="510500", name="中证500ETF", exchange="SH", sec_type="etf",
                market_type="A股", adjustment="none", source="test",
            ),
            settings={}, now=now, calendar=cal, fetchers={"etf": fake},
            pool_symbols=set(),  # settings 中已不在池
        )
        assert result.source == "ohlcv_daily"

    def test_no_new_rows_keeps_stale_note(self, db):
        cal, days, now = self._cal_and_now()
        db.replace_diag_prices(
            "600519",
            _make_prices("600519", days[:5], [9.0] * 5).with_columns([
                pl.lit("sina_stock").alias("source"),
                pl.lit("hfq").alias("adjustment"),
            ]),
        )
        fake = CountingFetcher(lambda symbol: _make_prices(symbol, days[:5], [9.0] * 5))
        result = ensure_research_prices(
            db, STOCK, settings={}, now=now, calendar=cal, fetchers={"stock": fake},
        )
        assert result.status == "stale"
        assert any("无新增数据" in n for n in result.notes)


class TestEquityEvents:
    def _insert_event(self, db, day, ratio, verified):
        db.upsert_equity_events(pl.DataFrame([{
            "symbol": "512000", "event_date": day, "event_type": "split_implied",
            "ratio": ratio, "ratio_semantics": "price_factor",
            "source": "inferred_from_price", "inferred": not verified,
            "verified": verified, "method": "test", "note": "test",
        }]))

    def test_hfq_stock_not_applicable(self, db):
        days = _weekdays(date(2026, 8, 3), 5)
        result = check_equity_events(db, STOCK, _make_prices("600519", days, [10.0] * 5))
        assert result.status == "not_applicable"

    def test_unverified_event_pauses(self, db):
        days = _weekdays(date(2026, 8, 3), 6)
        self._insert_event(db, days[3], 2.0, verified=False)
        closes = [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]  # 事件日价格减半
        result = check_equity_events(db, ETF, _make_prices("512000", days, closes))
        assert result.status == "paused"
        assert len(result.unverified) == 1
        assert result.adjusted_prices is None

    def test_unregistered_jump_is_data_issue(self, db):
        days = _weekdays(date(2026, 8, 3), 6)
        closes = [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]
        result = check_equity_events(db, ETF, _make_prices("512000", days, closes))
        assert result.status == "data_issue"
        assert len(result.unregistered_jumps) == 1
        assert result.unregistered_jumps[0]["date"] == days[3].isoformat()

    def test_verified_events_give_adjusted_series(self, db):
        days = _weekdays(date(2026, 8, 3), 6)
        self._insert_event(db, days[3], 2.0, verified=True)
        closes = [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]
        result = check_equity_events(db, ETF, _make_prices("512000", days, closes))
        assert result.status == "ok"
        assert result.adjusted_prices is not None
        adjusted = result.adjusted_prices.sort("date")
        adj_closes = adjusted["close"].to_list()
        # 事件前收盘 10/2 = 5 → 全序列连续 (无跳变)
        assert adj_closes == [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        returns = [
            adj_closes[i] / adj_closes[i - 1] - 1 for i in range(1, len(adj_closes))
        ]
        assert all(abs(r) < 1e-9 for r in returns)

    def test_registered_event_nearby_suppresses_jump(self, db):
        days = _weekdays(date(2026, 8, 3), 6)
        self._insert_event(db, days[3], 2.0, verified=False)
        closes = [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]
        result = check_equity_events(db, ETF, _make_prices("512000", days, closes))
        assert result.status == "paused"           # 不是 data_issue
        assert result.unregistered_jumps == []

    def test_clean_series_ok(self, db):
        days = _weekdays(date(2026, 8, 3), 6)
        closes = [10.0, 10.1, 9.9, 10.2, 10.0, 10.3]
        result = check_equity_events(db, ETF, _make_prices("512000", days, closes))
        assert result.status == "ok"
        assert result.adjusted_prices is None

    def test_detect_unregistered_jumps_direct(self, db):
        days = _weekdays(date(2026, 8, 3), 4)
        closes = [10.0, 10.0, 20.0, 20.0]
        jumps = detect_unregistered_jumps(
            _make_prices("512000", days, closes), ETF, settings={},
        )
        assert len(jumps) == 1
        assert jumps[0]["ret"] == pytest.approx(1.0)

    def test_adjust_multiple_events_cumulative(self):
        days = _weekdays(date(2026, 8, 3), 5)
        closes = [40.0, 20.0, 20.0, 10.0, 10.0]
        prices = _make_prices("512000", days, closes)
        events = [
            {"event_date": days[1], "ratio": 2.0},
            {"event_date": days[3], "ratio": 2.0},
        ]
        adjusted = adjust_prices_for_events(prices, events).sort("date")
        assert adjusted["close"].to_list() == [10.0, 10.0, 10.0, 10.0, 10.0]


class TestPriceLimit:
    def test_limits(self):
        assert price_limit_pct("600519", "stock") == 0.10
        assert price_limit_pct("300750", "stock") == 0.20
        assert price_limit_pct("688981", "stock") == 0.20
        assert price_limit_pct("920992", "stock") == 0.30
        assert price_limit_pct("510300", "etf") == 0.10


class TestNoTradingSideEffects:
    """取数 + 事件检查全链路不产生交易类写入 (P11-M2-06)."""

    TRADING_TABLES = (
        "accounts", "account_events", "position_lots", "account_snapshots",
        "targets", "target_weights", "trade_proposals", "execution_feedback",
        "trade_signals", "universe_members", "instruments",
        "strategy_states",
    )

    def test_full_cycle_writes_only_price_cache(self, db):
        days = _weekdays(date(2026, 8, 3), 20)
        cal = TradingCalendar(days=tuple(days), source="sina")
        now = datetime.combine(days[-1], dt_time(20, 0))
        fake = CountingFetcher(
            lambda symbol: _make_prices(symbol, days, [10.0] * len(days))
        )

        before = {t: db.get_table_count(t) for t in self.TRADING_TABLES}
        result = ensure_research_prices(
            db, ETF, settings={}, now=now, calendar=cal, fetchers={"etf": fake},
        )
        check = check_equity_events(db, ETF, result.prices)
        after = {t: db.get_table_count(t) for t in self.TRADING_TABLES}

        assert before == after == {t: 0 for t in self.TRADING_TABLES}
        assert db.get_table_count("diag_price_daily") == len(days)
        assert check.status == "ok"


class TestDefaults:
    def test_min_history_default_covers_ma200_and_momentum(self):
        assert DEFAULT_MIN_HISTORY_DAYS >= 252 + 21
