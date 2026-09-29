"""诊断页面数据层测试 (P11-M5; 只读装配, 手工期望值)."""

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from dashboard.diagnostics_data import (
    build_kline_frame,
    build_list_rows,
    load_holdings,
    load_snapshot_details,
    load_watchlist_rows,
    months_before,
    read_symbol_prices,
)
from data_pipeline.duckdb_manager import DuckDBManager


def _weekdays(start: date, n: int) -> list[date]:
    days, cur = [], start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _prices(symbol: str, days: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol, "date": d, "open": c, "high": c * 1.01,
            "low": c * 0.99, "close": c, "volume": 1e6, "amount": 1e7,
        }
        for d, c in zip(days, closes, strict=True)
    ])


@pytest.fixture
def db(tmp_path):
    db = DuckDBManager(str(tmp_path / "uidata.duckdb"))
    db.init_database()
    yield db
    db.close()


class TestMonthsBefore:
    def test_basic(self):
        assert months_before(date(2026, 9, 29), 6) == date(2026, 3, 29)

    def test_month_end_clamp(self):
        assert months_before(date(2026, 8, 31), 6) == date(2026, 2, 28)
        assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)

    def test_year_wrap(self):
        assert months_before(date(2026, 1, 15), 3) == date(2025, 10, 15)
        assert months_before(date(2026, 2, 28), 14) == date(2024, 12, 28)


class TestKlineFrame:
    def _series(self, n: int = 300) -> tuple[list[date], list[float]]:
        days = _weekdays(date(2024, 1, 1), n)
        closes = [100.0 + i * 0.1 for i in range(n)]
        return days, closes

    def test_display_window_is_six_months(self):
        days, closes = self._series()
        frame = build_kline_frame(_prices("600519", days, closes))
        cutoff = months_before(days[-1], 6)
        assert frame["date"].min() >= cutoff
        assert frame["date"].max() == days[-1]
        assert frame.height < len(days)                     # 确实裁剪了显示范围
        assert frame.height == len([d for d in days if d >= cutoff])

    def test_ma_computed_on_full_history(self):
        """显示窗口外的历史参与预热 (先裁剪再算 MA 会得到空值/不同值)."""
        days, closes = self._series()
        frame = build_kline_frame(_prices("600519", days, closes))
        first_displayed = frame["date"][0]
        index = days.index(first_displayed)
        assert index >= 20  # 显示起点前有足够历史 (否则本断言无意义)
        # 首个显示行的 MA20 必须使用窗口之前的收盘价
        expected_ma20 = sum(closes[index - 19 : index + 1]) / 20
        assert frame["ma20"][0] == pytest.approx(expected_ma20)
        # MA200 在显示窗口内历史补足后与全历史口径一致
        target = days[250]
        assert target in frame["date"].to_list()
        pos = frame["date"].to_list().index(target)
        expected_ma200 = sum(closes[51:251]) / 200
        assert frame["ma200"][pos] == pytest.approx(expected_ma200)

    def test_zero_width_bollinger_no_crash(self):
        days, _ = self._series(250)
        frame = build_kline_frame(_prices("510300", days, [10.0] * 250))
        assert frame["boll_mid"].to_list()[-1] == pytest.approx(10.0)
        assert frame["boll_upper"].to_list()[-1] == pytest.approx(10.0)

    def test_empty_passthrough(self):
        assert build_kline_frame(pl.DataFrame()).is_empty()


class TestReadPrices:
    def test_prefers_pool_then_diag(self, db, tmp_path):
        days = _weekdays(date(2026, 1, 1), 5)
        db.upsert_ohlcv(_prices("510300", days, [4.0] * 5))
        db.upsert_diag_prices(
            _prices("600519", days, [1500.0] * 5).with_columns([
                pl.lit("sina_stock").alias("source"),
                pl.lit("hfq").alias("adjustment"),
            ])
        )
        db_path = str(db.db_path)
        db.close()  # 读取走只读连接 (与写连接不混用)
        pool = read_symbol_prices(db_path, "510300")
        assert pool["close"].to_list() == [4.0] * 5
        diag = read_symbol_prices(db_path, "600519")
        assert diag["close"].to_list() == [1500.0] * 5
        missing = read_symbol_prices(db_path, "999999")
        assert missing.is_empty()

    def test_missing_db_returns_empty(self, tmp_path):
        assert read_symbol_prices(str(tmp_path / "nope.duckdb"), "510300").is_empty()


class TestHoldings:
    def _seed_account(self, db, path: str, days: list[date]) -> None:
        from data_pipeline.accounting import AccountEvent, AccountService

        db.upsert_ohlcv(_prices("510300", days, [4.0 + i * 0.001 for i in range(len(days))]))
        svc = AccountService(db)
        svc.ensure_account("REAL-1", "项目账户", mode="real")
        svc.import_events([
            AccountEvent(
                event_id="init-1", account_id="REAL-1", event_type="initial",
                event_time=datetime(days[0].year, days[0].month, days[0].day),
                amount=100000.0,
            ),
            AccountEvent(
                event_id="buy-1", account_id="REAL-1", event_type="trade",
                event_time=datetime(days[0].year, days[0].month, days[0].day, 15),
                symbol="510300", side="BUY", shares=1000.0, price=4.0, fee=0.0,
            ),
        ])

    def test_reads_positions_with_weight(self, db):
        days = _weekdays(date(2026, 1, 1), 5)
        self._seed_account(db, str(db.db_path), days)
        db_path = str(db.db_path)
        db.close()  # 读取层用只读连接
        settings = {"universe": {"broad_base": [
            {"symbol": "510300", "name": "沪深300ETF"},
        ]}}
        result = load_holdings(db_path, "REAL-1", settings)
        assert result["ok"] is True
        assert result["mode"] == "real"
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        last_close = 4.0 + 4 * 0.001
        # 手工核对: 现金 100000-4000, 市值 1000×last_close
        expected_total = 100000.0 - 4000.0 + 1000.0 * last_close
        assert result["total"] == pytest.approx(expected_total)
        assert row["weight"] == pytest.approx(1000.0 * last_close / expected_total)
        assert row["name"] == "沪深300ETF"
        assert row["price_date"] == days[-1].isoformat()

    def test_no_account_configured(self, db):
        result = load_holdings(str(db.db_path), None)
        assert result["ok"] is False
        assert "未配置账户" in result["reason"]

    def test_unknown_account(self, db):
        days = _weekdays(date(2026, 1, 1), 3)
        self._seed_account(db, str(db.db_path), days)
        db_path = str(db.db_path)
        db.close()
        result = load_holdings(db_path, "REAL-9")
        assert result["ok"] is False
        assert "不存在" in result["reason"]


class TestListRows:
    def _entry(self, symbol, name, source="观察", weight=None):
        return {"symbol": symbol, "name": name, "source": source, "weight": weight}

    def test_sorting_change_and_reason(self):
        defense = {
            "score": 66.0, "status": "防御", "completeness": "5/5",
            "as_of": date(2026, 9, 25), "trigger": "新增防御",
            "metrics": {"hints": ["RSI 超买"], "data_status": "fresh"},
            "checks": {"missing_required": []},
        }
        watch = {
            "score": 45.0, "status": "警戒", "completeness": "5/5",
            "as_of": date(2026, 9, 25), "trigger": "升级待确认",
            "metrics": {"hints": [], "data_status": "fresh"},
            "checks": {"missing_required": ["ma200_dev: 指标缺失"]},
        }
        steady = {
            "score": 10.0, "status": "正常", "completeness": "5/5",
            "as_of": date(2026, 9, 25), "trigger": "维持",
            "metrics": {"hints": [], "data_status": "fresh"},
            "checks": {"missing_required": []},
        }
        details = {
            "600519": {"latest": defense, "previous": {"status": "警戒"}},
            "510300": {"latest": watch, "previous": {"status": "正常"}},
            "159915": {"latest": steady, "previous": {"status": "正常"}},
            "513100": None,
        }
        rows = build_list_rows(
            [self._entry("510300", "沪深300ETF"), self._entry("513100", "纳指ETF"),
             self._entry("159915", "创业板ETF"),
             self._entry("600519", "贵州茅台", source="持仓", weight=0.3)],
            details,
        )
        codes = rows["代码"].to_list()
        assert codes == ["600519", "510300", "159915", "513100"]  # 防御>警戒>正常>未评估
        by_code = {row["代码"]: row for row in rows.to_dicts()}
        assert by_code["600519"]["较上次变化"] == "警戒 → 防御"  # 跨档 → 记录状态变化
        assert by_code["510300"]["较上次变化"] == "正常 → 警戒"  # 跨档 → 记录状态变化
        assert by_code["159915"]["较上次变化"] == "维持"        # 同档 → 记录触发词
        assert "ma200_dev" in by_code["510300"]["主要原因"]     # 缺项优先于提示
        assert by_code["600519"]["权重"] == "30.0%"
        assert by_code["513100"]["状态"] is None
        assert by_code["513100"]["较上次变化"] is None

    def test_empty_entries(self):
        assert build_list_rows([], {}).is_empty()


class TestSnapshotAndWatchlistReaders:
    def test_snapshot_details_reads_two_most_recent(self, db):
        for day, status in ((date(2026, 9, 24), "正常"), (date(2026, 9, 25), "警戒")):
            db.upsert_diagnostic_snapshot({
                "snapshot_id": f"510300|{day}|r|p", "symbol": "510300",
                "as_of": day, "rules_version": "r", "price_version": "p",
                "metrics_json": "{}", "contributions_json": "{}",
                "checks_json": "{}", "score": 40.0, "status": status,
                "completeness": "5/5", "trigger": "初始",
            })
        db_path = str(db.db_path)
        db.close()
        details = load_snapshot_details(db_path, ["510300", "600519"])
        assert details["510300"]["latest"]["status"] == "警戒"
        assert details["510300"]["previous"]["status"] == "正常"
        assert details["600519"]["latest"] is None

    def test_watchlist_reader(self, db):
        db_path = str(db.db_path)
        assert db.get_watchlist("active").is_empty()  # 初始为空 (写连接上确认)
        db.add_watchlist("510500", name="中证500ETF", exchange="SH", sec_type="etf")
        db.close()
        rows = load_watchlist_rows(db_path)
        assert rows["symbol"].to_list() == ["510500"]
