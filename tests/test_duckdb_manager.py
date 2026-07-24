"""DuckDBManager 单元测试.

验证: 表创建、OHLCV 幂等写入、UPSERT 更新、元数据读写、查询接口.
"""

import os
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from data_pipeline.duckdb_manager import DuckDBManager


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
