"""DuckDBManager 单元测试.

验证: 表创建、OHLCV 幂等写入、UPSERT 更新、元数据读写、查询接口.
"""

import os
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from data_pipeline.duckdb_manager import SCHEMA_VERSION, DuckDBManager


@pytest.fixture
def db(tmp_path):
    """创建临时数据库."""
    db_path = str(tmp_path / "test.duckdb")
    db = DuckDBManager(db_path)
    db.init_database()
    yield db
    db.close()


@pytest.fixture
def sample_ohlcv():
    """生成合成 OHLCV 数据."""
    np.random.seed(42)
    n = 100
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]
    prices = 3.5 * np.cumprod(1 + np.random.normal(0.0002, 0.012, n))

    return pl.DataFrame({
        "symbol": ["510300"] * n,
        "date": dates,
        "open": prices * (1 + np.random.uniform(-0.005, 0.005, n)),
        "high": prices * (1 + np.abs(np.random.uniform(0, 0.015, n))),
        "low": prices * (1 - np.abs(np.random.uniform(0, 0.015, n))),
        "close": prices,
        "volume": np.random.uniform(1e7, 3e7, n),
        "amount": np.random.uniform(1e7, 3e7, n) * prices,
    })


@pytest.fixture
def sample_metadata():
    """生成合成 ETF 元数据."""
    return pl.DataFrame([{
        "symbol": "510300",
        "name": "沪深300ETF",
        "market_type": "A股",
        "settlement_type": "T+1",
        "list_date": date(2012, 5, 28),
        "avg_daily_volume_20d": 2.0e7,
    }])


class TestDatabaseInit:
    """数据库初始化测试."""

    def test_all_tables_created(self, db):
        """验证 6 张表全部创建."""
        expected = [
            "ohlcv_daily", "etf_metadata", "macro_indicators",
            "trade_signals", "account_state", "schema_version",
        ]
        for table in expected:
            assert db.table_exists(table), f"Table '{table}' should exist"

    def test_schema_version_recorded(self, db):
        """验证 schema 版本已记录."""
        count = db.get_table_count("schema_version")
        assert count >= 1, "schema_version should have at least 1 row"


class TestOHLCVUpsert:
    """OHLCV 写入测试."""

    def test_write_and_count(self, db, sample_ohlcv):
        """写入数据后行数正确."""
        db.upsert_ohlcv(sample_ohlcv)
        assert db.get_table_count("ohlcv_daily") == len(sample_ohlcv)

    def test_idempotent(self, db, sample_ohlcv):
        """重复写入不产生重复行."""
        db.upsert_ohlcv(sample_ohlcv)
        count1 = db.get_table_count("ohlcv_daily")
        db.upsert_ohlcv(sample_ohlcv)
        count2 = db.get_table_count("ohlcv_daily")
        assert count1 == count2, f"Idempotent fail: {count1} != {count2}"

    def test_upsert_update(self, db, sample_ohlcv):
        """UPSERT 更新已存在行而非插入新行."""
        db.upsert_ohlcv(sample_ohlcv)
        count1 = db.get_table_count("ohlcv_daily")

        # 修改最后一行 close 值
        last = sample_ohlcv.tail(1).with_columns(close=pl.lit(999.0))
        db.upsert_ohlcv(last)
        count2 = db.get_table_count("ohlcv_daily")

        assert count2 == count1, f"UPSERT update changed row count: {count2} != {count1}"

    def test_missing_columns_raises(self, db):
        """缺少必需列应抛出 ValueError."""
        bad_df = pl.DataFrame({"symbol": ["510300"], "date": [date(2024, 1, 1)]})
        with pytest.raises(ValueError, match="missing required columns"):
            db.upsert_ohlcv(bad_df)

    def test_empty_df_noop(self, db):
        """空 DataFrame 不应报错."""
        db.upsert_ohlcv(pl.DataFrame())
        assert db.get_table_count("ohlcv_daily") == 0


class TestReadBack:
    """数据读回测试."""

    def test_get_ohlcv_all(self, db, sample_ohlcv):
        """读回全部数据."""
        db.upsert_ohlcv(sample_ohlcv)
        result = db.get_ohlcv()
        assert len(result) == len(sample_ohlcv)

    def test_get_ohlcv_filter_symbol(self, db, sample_ohlcv):
        """按标的过滤."""
        db.upsert_ohlcv(sample_ohlcv)
        result = db.get_ohlcv(symbols=["510300"])
        assert len(result) == len(sample_ohlcv)

    def test_get_ohlcv_filter_date(self, db, sample_ohlcv):
        """按日期过滤."""
        db.upsert_ohlcv(sample_ohlcv)
        result = db.get_ohlcv(start_date="2024-02-01", end_date="2024-02-29")
        assert len(result) > 0
        for row_date in result["date"].to_list():
            assert date(2024, 2, 1) <= row_date <= date(2024, 2, 29)

    def test_get_ohlcv_nonexistent_symbol(self, db, sample_ohlcv):
        """查询不存在的标的不报错."""
        db.upsert_ohlcv(sample_ohlcv)
        result = db.get_ohlcv(symbols=["999999"])
        assert len(result) == 0


class TestMetadata:
    """ETF 元数据测试."""

    def test_write_and_read(self, db, sample_metadata):
        """写入并读回元数据."""
        db.upsert_etf_metadata(sample_metadata)
        result = db.get_etf_metadata(symbols=["510300"])
        assert len(result) == 1
        assert result["name"][0] == "沪深300ETF"
        assert result["settlement_type"][0] == "T+1"


class TestUtilities:
    """工具方法测试."""

    def test_get_date_range(self, db, sample_ohlcv):
        """日期范围查询."""
        db.upsert_ohlcv(sample_ohlcv)
        start, end = db.get_date_range()
        assert start is not None
        assert end is not None
        assert start <= end

    def test_get_symbols(self, db, sample_ohlcv):
        """获取所有标的."""
        db.upsert_ohlcv(sample_ohlcv)
        symbols = db.get_symbols()
        assert "510300" in symbols


class TestSchemaMigration:
    """schema v1 → v2: 策略版本列 + 旧记录标注 (A0-1, plan §5 只增列)."""

    def _make_v1_db(self, tmp_path) -> str:
        """构造 v1 结构的旧库: trade_signals 无 strategy_version 列."""
        import duckdb

        db_path = str(tmp_path / "legacy.duckdb")
        con = duckdb.connect(db_path)
        con.execute("""
            CREATE TABLE trade_signals (
                signal_id BIGINT PRIMARY KEY,
                signal_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                direction VARCHAR NOT NULL,
                target_weight DOUBLE,
                trigger_reason VARCHAR,
                executed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                description VARCHAR
            )
        """)
        con.execute(
            "INSERT INTO schema_version (version, description) VALUES (1, 'v1')"
        )
        con.execute("""
            INSERT INTO trade_signals
                (signal_id, signal_date, symbol, direction,
                 target_weight, trigger_reason)
            VALUES (20260911001, DATE '2026-09-11', '510300', 'HOLD', 0.0, '旧口径信号')
        """)
        con.close()
        return db_path

    def test_migrates_v1_to_v2_and_marks_legacy(self, tmp_path):
        db_path = self._make_v1_db(tmp_path)
        db = DuckDBManager(db_path)
        db.init_database()  # 迁移在此执行

        # 旧行内容不被改写, 仅新增版本标注 v0.1
        row = db.conn.execute(
            "SELECT direction, trigger_reason, strategy_version "
            "FROM trade_signals WHERE signal_id = 20260911001"
        ).fetchone()
        assert row[1] == "旧口径信号"
        assert row[2] == "v0.1"

        # 新写入携带 v0.2
        db.upsert_signals(pl.DataFrame([{
            "signal_id": 20260912001, "signal_date": date(2026, 9, 12),
            "symbol": "510050", "direction": "BUY", "target_weight": 0.1,
            "trigger_reason": "新口径", "strategy_version": "v0.2",
        }]))
        row2 = db.conn.execute(
            "SELECT strategy_version FROM trade_signals WHERE signal_id = 20260912001"
        ).fetchone()
        assert row2[0] == "v0.2"
        db.close()

    def test_migration_idempotent(self, tmp_path):
        db_path = self._make_v1_db(tmp_path)
        for _ in range(2):
            db = DuckDBManager(db_path)
            db.init_database()
            db.close()
        db = DuckDBManager(db_path)
        versions = db.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        db.close()
        assert [v[0] for v in versions] == list(range(1, SCHEMA_VERSION + 1))

    def test_upsert_without_version_keeps_existing(self, db):
        """旧调用方不传版本列时, 更新不覆盖已记录的版本 (COALESCE 保护)."""
        db.upsert_signals(pl.DataFrame([{
            "signal_id": 1, "signal_date": date(2026, 9, 1),
            "symbol": "510300", "direction": "BUY", "target_weight": 0.1,
            "trigger_reason": "v0.2 写入", "strategy_version": "v0.2",
        }]))
        db.upsert_signals(pl.DataFrame([{
            "signal_id": 1, "signal_date": date(2026, 9, 1),
            "symbol": "510300", "direction": "HOLD", "target_weight": 0.0,
            "trigger_reason": "旧签名更新",
        }]))
        row = db.conn.execute(
            "SELECT direction, strategy_version FROM trade_signals WHERE signal_id = 1"
        ).fetchone()
        assert row[0] == "HOLD"
        assert row[1] == "v0.2"


class TestV3TablesAndRuns:
    """schema v3: 账户/四类记录/运行记录表与迁移 (M2)."""

    def test_v3_tables_created(self, db):
        for table in (
            "accounts", "account_events", "position_lots", "account_snapshots",
            "targets", "target_weights", "trade_proposals", "execution_feedback",
            "runs", "experiments", "schema_migration_log",
            "instruments", "universe_versions", "universe_members", "nav_daily",
        ):
            assert db.table_exists(table), f"缺少表: {table}"

    def test_chained_migration_v1_to_current(self, tmp_path):
        db_path = TestSchemaMigration()._make_v1_db(tmp_path)
        db = DuckDBManager(db_path)
        db.init_database()
        versions = [
            row[0] for row in
            db.conn.execute("SELECT version FROM schema_version ORDER BY 1").fetchall()
        ]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
        assert db.table_exists("account_events")
        assert db.table_exists("instruments")
        assert db.table_exists("strategy_states")
        assert db.table_exists("equity_events")
        assert db.table_exists("securities")
        assert db.table_exists("diagnostic_snapshots")
        logs = db.conn.execute(
            "SELECT version, status FROM schema_migration_log ORDER BY version"
        ).fetchall()
        assert (3, "applied") in logs
        assert (4, "applied") in logs
        assert (5, "applied") in logs
        assert (6, "applied") in logs
        assert (7, "applied") in logs
        # v6: experiments 追踪列已补齐
        cols = {
            row[1] for row in db.conn.execute(
                "PRAGMA table_info('experiments')"
            ).fetchall()
        }
        assert {"mode", "started_at", "finished_at", "error"} <= cols
        # 迁移不丢失 v1 事实
        assert db.get_table_count("trade_signals") == 1
        db.close()

    def test_run_record_push_update(self, db):
        db.insert_run(pl.DataFrame([{
            "run_id": "daily-X", "run_type": "daily", "status": "ok",
            "run_date": date(2026, 9, 11), "mode": "research",
            "strategy_version": "v0.2", "data_version": "2026-09-11",
        }]))
        db.update_run_push("daily-X", False, 3, "network error")
        row = db.get_run("daily-X")
        assert row is not None
        assert row["push_ok"] is False
        assert row["push_attempts"] == 3
        assert row["push_error"] == "network error"

    def test_transaction_rolls_back_on_error(self, db):
        with pytest.raises(RuntimeError), db.transaction():
            db.upsert_account(pl.DataFrame([{
                "account_id": "TX-1", "name": "x", "mode": "paper",
            }]))
            raise RuntimeError("boom")
        assert db.get_accounts().is_empty()
