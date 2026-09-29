"""Phase 1.1 诊断存储单元测试 (schema v8, P11-M2).

验证: securities/watchlist/watchlist_changes/diag_price_daily/diagnostic_snapshots
的 CRUD、幂等性、修订语义与"与交易链隔离"(不写账户/目标/建议/执行反馈)。
"""

from datetime import date

import polars as pl
import pytest

from data_pipeline.duckdb_manager import SCHEMA_VERSION, DuckDBManager


@pytest.fixture
def db(tmp_path):
    db = DuckDBManager(str(tmp_path / "v8.duckdb"))
    db.init_database()
    yield db
    db.close()


def _prices(symbol: str, start: date, n: int, base: float = 10.0) -> pl.DataFrame:
    from datetime import timedelta

    dates = [start + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": [base + i * 0.01 for i in range(n)],
        "high": [base + i * 0.01 + 0.05 for i in range(n)],
        "low": [base + i * 0.01 - 0.05 for i in range(n)],
        "close": [base + i * 0.01 + 0.02 for i in range(n)],
        "volume": [1e6] * n,
        "amount": [1e7] * n,
        "source": ["sina_etf"] * n,
        "adjustment": ["none"] * n,
    })


class TestSchemaV8:
    def test_version_and_tables(self, db):
        assert SCHEMA_VERSION == 8
        for table in (
            "securities", "watchlist", "watchlist_changes",
            "diag_price_daily", "diagnostic_snapshots",
        ):
            assert db.table_exists(table), table

    def test_migration_idempotent_and_logged(self, db):
        versions_before = db.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        counts_before = {
            t: db.get_table_count(t)
            for t in ("ohlcv_daily", "securities", "diagnostic_snapshots")
        }
        db.init_database()  # 二次执行必须幂等
        versions_after = db.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        assert versions_before == versions_after
        assert counts_before == {
            t: db.get_table_count(t)
            for t in ("ohlcv_daily", "securities", "diagnostic_snapshots")
        }
        log = db.conn.execute(
            "SELECT status FROM schema_migration_log WHERE version = 8"
        ).fetchall()
        assert log == [("applied",)]


class TestSecurities:
    def test_upsert_insert_and_get(self, db):
        n = db.upsert_securities(pl.DataFrame([{
            "symbol": "600519", "name": "贵州茅台", "exchange": "SH",
            "sec_type": "stock", "market_type": "A股", "adjustment": "hfq",
            "source": "sina_stock_list",
        }]))
        assert n == 1
        row = db.get_security("600519")
        assert row["name"] == "贵州茅台"
        assert row["adjustment"] == "hfq"
        assert row["sec_type"] == "stock"

    def test_upsert_partial_update_keeps_existing(self, db):
        db.upsert_securities(pl.DataFrame([{
            "symbol": "510300", "name": "沪深300ETF", "exchange": "SH",
            "sec_type": "etf", "adjustment": "none",
        }]))
        # 后续写入不带 name → COALESCE 保留旧名称
        db.upsert_securities(pl.DataFrame([{"symbol": "510300", "source": "refresh"}]))
        row = db.get_security("510300")
        assert row["name"] == "沪深300ETF"
        assert row["source"] == "refresh"
        assert db.get_table_count("securities") == 1

    def test_get_securities_filter_and_search(self, db):
        db.upsert_securities(pl.DataFrame([
            {"symbol": "600519", "name": "贵州茅台"},
            {"symbol": "510300", "name": "沪深300ETF"},
            {"symbol": "159915", "name": "创业板ETF"},
        ]))
        filtered = db.get_securities(["600519", "159915"])
        assert filtered["symbol"].to_list() == ["159915", "600519"]
        found = db.find_securities_by_name("300")
        assert found["symbol"].to_list() == ["510300"]
        assert db.get_security("999999") is None


class TestWatchlist:
    def test_add_is_idempotent_and_logged(self, db):
        db.add_watchlist("510500", name="中证500ETF", exchange="SH", sec_type="etf", note="观察")
        db.add_watchlist("510500", name="中证500ETF", exchange="SH", sec_type="etf")  # 重复添加
        assert db.get_table_count("watchlist") == 1
        changes = db.get_watchlist_changes("510500")
        assert changes["action"].to_list() == ["add"]  # 不重复记录

    def test_remove_reactivate_and_history(self, db):
        db.add_watchlist("600036", name="招商银行", sec_type="stock")
        db.remove_watchlist("600036", reason="不再跟踪")
        db.remove_watchlist("600036")  # 幂等
        active = db.get_watchlist("active")
        assert active.is_empty()
        removed = db.get_watchlist("removed")
        assert removed["symbol"].to_list() == ["600036"]
        db.add_watchlist("600036", name="招商银行")  # 恢复
        assert db.get_watchlist("active")["symbol"].to_list() == ["600036"]
        actions = db.get_watchlist_changes("600036")["action"].to_list()
        # 最新在前: add(恢复) → remove → add(初始)
        assert actions == ["add", "remove", "add"]
        detail = db.get_watchlist_changes("600036")["detail"].to_list()
        assert "恢复" in detail[0]

    def test_note_update_and_errors(self, db):
        with pytest.raises(ValueError):
            db.update_watchlist_note("000001", "不存在")
        db.add_watchlist("000001", name="平安银行", sec_type="stock", note="旧")
        db.update_watchlist_note("000001", "新备注")
        row = db.get_watchlist("active").filter(pl.col("symbol") == "000001")
        assert row["note"].to_list() == ["新备注"]
        actions = db.get_watchlist_changes("000001")["action"].to_list()
        assert "note_update" in actions


class TestDiagPrices:
    def test_upsert_and_query_range(self, db):
        df = _prices("600519", date(2026, 9, 1), 5, base=1500.0)
        assert db.upsert_diag_prices(df) == 5
        # 重复写入幂等
        db.upsert_diag_prices(df)
        assert db.get_table_count("diag_price_daily") == 5
        subset = db.get_diag_prices("600519", start=date(2026, 9, 2), end=date(2026, 9, 3))
        assert subset["date"].to_list() == [date(2026, 9, 2), date(2026, 9, 3)]

    def test_upsert_updates_values(self, db):
        db.upsert_diag_prices(_prices("600519", date(2026, 9, 1), 2))
        revised = _prices("600519", date(2026, 9, 1), 2, base=2000.0)
        db.upsert_diag_prices(revised)
        row = db.get_diag_prices("600519").filter(
            pl.col("date") == date(2026, 9, 1)
        )
        assert row["close"].to_list() == [2000.02]
        assert db.get_table_count("diag_price_daily") == 2

    def test_replace_is_full_rebuild(self, db):
        db.upsert_diag_prices(_prices("600519", date(2026, 8, 1), 10))
        replacement = _prices("600519", date(2026, 9, 1), 3, base=100.0)
        assert db.replace_diag_prices("600519", replacement) == 3
        rows = db.get_diag_prices("600519")
        assert len(rows) == 3
        assert rows["date"].min() == date(2026, 9, 1)

    def test_delete(self, db):
        db.upsert_diag_prices(_prices("600519", date(2026, 9, 1), 4))
        assert db.delete_diag_prices("600519") == 4
        assert db.get_diag_prices("600519").is_empty()

    def test_missing_key_rejected(self, db):
        with pytest.raises(ValueError):
            db.upsert_diag_prices(pl.DataFrame({"symbol": ["600519"]}))


class TestDiagnosticSnapshots:
    def _row(self, symbol="510300", as_of=date(2026, 9, 25), score=42.0,
             status="警戒", trigger="初始"):
        return {
            "snapshot_id": DuckDBManager.diagnostic_snapshot_id(
                symbol, as_of, "diag-v0.1-research", "sina|none|2026-09-25"
            ),
            "symbol": symbol,
            "as_of": as_of,
            "rules_version": "diag-v0.1-research",
            "price_version": "sina|none|2026-09-25",
            "run_kind": "live",
            "metrics_json": '{"ma20": 4.1}',
            "contributions_json": '{"trend": 20}',
            "checks_json": '[]',
            "score": score,
            "status": status,
            "completeness": "full",
            "trigger": trigger,
        }

    def test_id_is_deterministic(self):
        a = DuckDBManager.diagnostic_snapshot_id(
            "510300", date(2026, 9, 25), "r1", "p1"
        )
        b = DuckDBManager.diagnostic_snapshot_id(
            "510300", date(2026, 9, 25), "r1", "p1"
        )
        assert a == b
        assert a != DuckDBManager.diagnostic_snapshot_id(
            "510300", date(2026, 9, 25), "r2", "p1"
        )

    def test_upsert_idempotent(self, db):
        db.upsert_diagnostic_snapshot(self._row())
        db.upsert_diagnostic_snapshot(self._row(score=45.0))
        assert db.get_table_count("diagnostic_snapshots") == 1
        latest = db.get_latest_diagnostic_snapshot("510300")
        assert latest["score"] == 45.0

    def test_history_and_latest_per_symbol(self, db):
        from datetime import timedelta

        as_of = date(2026, 9, 24)
        for i, status in enumerate(["正常", "警戒", "防御"]):
            row = self._row(as_of=as_of + timedelta(days=i), score=10.0 * i,
                            status=status)
            db.upsert_diagnostic_snapshot(row)
        db.upsert_diagnostic_snapshot(self._row(symbol="600519", as_of=as_of))
        latest = db.get_latest_diagnostic_snapshot("510300")
        assert latest["status"] == "防御"
        history = db.get_diagnostic_snapshots("510300")
        assert len(history) == 3
        assert history["as_of"].to_list()[0] == date(2026, 9, 26)
        per_symbol = db.get_latest_snapshot_per_symbol()
        assert per_symbol["symbol"].to_list() == ["510300", "600519"]
        row_600519 = per_symbol.filter(pl.col("symbol") == "600519")
        assert row_600519["status"].to_list() == ["警戒"]

    def test_missing_required_rejected(self, db):
        with pytest.raises(ValueError):
            db.upsert_diagnostic_snapshot({"symbol": "510300"})


class TestTradingChainIsolation:
    """诊断存储不得产生交易类写入 (P11-M2-06 的存储层部分)."""

    TRADING_TABLES = (
        "accounts", "account_events", "position_lots", "account_snapshots",
        "targets", "target_weights", "trade_proposals", "execution_feedback",
        "trade_signals", "universe_members", "instruments",
        "strategy_states", "equity_events",
    )

    def test_diag_writes_do_not_touch_trading_tables(self, db):
        before = {t: db.get_table_count(t) for t in self.TRADING_TABLES}

        db.upsert_securities(pl.DataFrame([{"symbol": "600519", "name": "贵州茅台"}]))
        db.add_watchlist("600519", name="贵州茅台", sec_type="stock")
        db.remove_watchlist("600519")
        db.upsert_diag_prices(_prices("600519", date(2026, 9, 1), 3))
        db.replace_diag_prices("600519", _prices("600519", date(2026, 9, 1), 2))
        db.upsert_diagnostic_snapshot({
            "snapshot_id": "600519|2026-09-25|r|p",
            "symbol": "600519", "as_of": date(2026, 9, 25),
        })

        after = {t: db.get_table_count(t) for t in self.TRADING_TABLES}
        assert before == after == {t: 0 for t in self.TRADING_TABLES}
