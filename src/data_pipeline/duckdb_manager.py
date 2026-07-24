"""DuckDB Manager — 单一真理之源 (Single Source of Truth).

负责 Quant-Pilot 所有持久化数据的读写操作:
  - 数据库初始化与 schema 版本管理
  - OHLCV 日线数据的幂等 UPSERT
  - ETF 元数据维护
  - 查询接口 (供策略/回测层调用)

规则:
  - 这是 DuckDB 的**唯一写入入口**, 禁止其他模块直连 .duckdb 文件
  - 所有写入操作必须幂等 — 重复执行不产生重复行/副作用
  - 连接生命周期由本模块管理, 调用方通过上下文管理器获取连接
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

import duckdb
import polars as pl

logger = logging.getLogger(__name__)

# ---- Schema 版本 ----
SCHEMA_VERSION = 1

# ---- DDL (建表语句) ----
_DDL_OHLCV = """
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    symbol    VARCHAR NOT NULL,
    date      DATE NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    volume    DOUBLE,
    amount    DOUBLE,
    adj_factor DOUBLE DEFAULT 1.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
)
"""

_DDL_ETF_META = """
CREATE TABLE IF NOT EXISTS etf_metadata (
    symbol               VARCHAR PRIMARY KEY,
    name                 VARCHAR NOT NULL,
    market_type          VARCHAR,
    settlement_type      VARCHAR,
    list_date            DATE,
    avg_daily_volume_20d DOUBLE,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_MACRO = """
CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator_name VARCHAR NOT NULL,
    stat_date      DATE NOT NULL,
    publish_date   DATE NOT NULL,
    value          DOUBLE,
    source         VARCHAR,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (indicator_name, stat_date)
)
"""

_DDL_SIGNALS = """
CREATE TABLE IF NOT EXISTS trade_signals (
    signal_id      BIGINT PRIMARY KEY,
    signal_date    DATE NOT NULL,
    symbol         VARCHAR NOT NULL,
    direction      VARCHAR NOT NULL,
    target_weight  DOUBLE,
    trigger_reason VARCHAR,
    executed       BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_ACCOUNT = """
CREATE TABLE IF NOT EXISTS account_state (
    as_of_date         DATE PRIMARY KEY,
    total_nav          DOUBLE,
    cash               DOUBLE,
    positions_json     VARCHAR,
    daily_return       DOUBLE,
    cumulative_return  DOUBLE,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_SCHEMA_VER = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description VARCHAR
)
"""

_ALL_DDL: list[tuple[str, str]] = [
    ("ohlcv_daily", _DDL_OHLCV),
    ("etf_metadata", _DDL_ETF_META),
    ("macro_indicators", _DDL_MACRO),
    ("trade_signals", _DDL_SIGNALS),
    ("account_state", _DDL_ACCOUNT),
    ("schema_version", _DDL_SCHEMA_VER),
]


class DuckDBManager:
    """DuckDB 数据库管理器.

    Usage:
        db = DuckDBManager("data/quant_pilot.duckdb")
        db.init_database()
        db.upsert_ohlcv(df)
        result = db.get_ohlcv(symbols=["510300"])
        db.close()
    """

    def __init__(self, db_path: str) -> None:
        """初始化管理器.

        Args:
            db_path: .duckdb 文件路径 (相对或绝对)
        """
        self.db_path = Path(db_path)
        self._conn: Optional[duckdb.DuckDBPyConnection] = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """惰性初始化连接."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(str(self.db_path))
            logger.debug("Connected to DuckDB: %s", self.db_path)
        return self._conn

    def close(self) -> None:
        """关闭数据库连接."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("DuckDB connection closed")

    @contextmanager
    def session(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """上下文管理器, 自动关闭连接."""
        try:
            yield self.conn
        finally:
            self.close()

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    def init_database(self) -> None:
        """创建所有表, 记录 schema 版本.

        幂等: 重复调用不会报错 (CREATE TABLE IF NOT EXISTS).
        """
        logger.info("Initializing DuckDB schema (v%d)...", SCHEMA_VERSION)

        for table_name, ddl in _ALL_DDL:
            self.conn.execute(ddl)
            logger.debug("  Ensured table: %s", table_name)

        # 记录 schema 版本 (幂等)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
            [SCHEMA_VERSION, "Initial schema v1: 6 tables for P0 MVP"],
        )

        logger.info("DuckDB schema ready")

    # ------------------------------------------------------------------
    # OHLCV 写入 (UPSERT — 幂等)
    # ------------------------------------------------------------------

    def upsert_ohlcv(self, df: pl.DataFrame) -> int:
        """插入或更新日线 OHLCV 数据.

        以 (symbol, date) 为主键, 已存在的行更新 OHLCV 值,
        不存在的行插入新记录. 重复执行幂等.

        Args:
            df: Polars DataFrame
                必须包含: symbol, date, open, high, low, close
                可选: volume, amount

        Returns:
            实际写入行数

        Raises:
            ValueError: 缺少必需列
        """
        if df.is_empty():
            logger.warning("upsert_ohlcv: empty DataFrame, skipping")
            return 0

        self._validate_ohlcv_columns(df)

        # 注册为临时视图
        pdf = df.to_pandas()
        self.conn.register("_upsert_ohlcv", pdf)

        try:
            # DuckDB UPSERT: ON CONFLICT (symbol, date) DO UPDATE
            self.conn.execute("""
                INSERT INTO ohlcv_daily
                    (symbol, date, open, high, low, close, volume, amount)
                SELECT
                    symbol, date, open, high, low, close, volume, amount
                FROM _upsert_ohlcv
                ON CONFLICT (symbol, date) DO UPDATE SET
                    open      = EXCLUDED.open,
                    high      = EXCLUDED.high,
                    low       = EXCLUDED.low,
                    close     = EXCLUDED.close,
                    volume    = EXCLUDED.volume,
                    amount    = EXCLUDED.amount
            """)
        finally:
            self.conn.unregister("_upsert_ohlcv")

        row_count = len(df)
        logger.info("upsert_ohlcv: %d rows written", row_count)
        return row_count

    @staticmethod
    def _validate_ohlcv_columns(df: pl.DataFrame) -> None:
        """校验 OHLCV DataFrame 包含必要列."""
        required = {"symbol", "date", "open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"OHLCV DataFrame missing required columns: {missing}. "
                f"Got: {sorted(df.columns)}"
            )

    # ------------------------------------------------------------------
    # ETF 元数据写入
    # ------------------------------------------------------------------

    def upsert_etf_metadata(self, df: pl.DataFrame) -> int:
        """插入或更新 ETF 元数据.

        Args:
            df: 包含 symbol, name, market_type, settlement_type 等列

        Returns:
            写入行数
        """
        if df.is_empty():
            return 0

        pdf = df.to_pandas()
        self.conn.register("_upsert_meta", pdf)

        try:
            self.conn.execute("""
                INSERT INTO etf_metadata
                    (symbol, name, market_type, settlement_type, list_date,
                     avg_daily_volume_20d)
                SELECT
                    symbol, name, market_type, settlement_type, list_date,
                    avg_daily_volume_20d
                FROM _upsert_meta
                ON CONFLICT (symbol) DO UPDATE SET
                    name                 = EXCLUDED.name,
                    market_type          = EXCLUDED.market_type,
                    settlement_type      = EXCLUDED.settlement_type,
                    list_date            = COALESCE(EXCLUDED.list_date, etf_metadata.list_date),
                    avg_daily_volume_20d = COALESCE(EXCLUDED.avg_daily_volume_20d, etf_metadata.avg_daily_volume_20d)
            """)
        finally:
            self.conn.unregister("_upsert_meta")

        row_count = len(df)
        logger.info("upsert_etf_metadata: %d records", row_count)
        return row_count

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_ohlcv(
        self,
        symbols: Optional[list[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pl.DataFrame:
        """查询日线 OHLCV 数据.

        Args:
            symbols: 标的列表, None = 全部
            start_date: 起始日期 "YYYY-MM-DD", None = 不限
            end_date: 截止日期 "YYYY-MM-DD", None = 不限

        Returns:
            Polars DataFrame, 按 (symbol, date) 排序
        """
        query = "SELECT * FROM ohlcv_daily WHERE 1=1"
        params: list = []

        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)

        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY symbol, date"

        result = self.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()

        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    def get_etf_metadata(
        self,
        symbols: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """查询 ETF 元数据."""
        if symbols:
            placeholders = ", ".join(["?" for _ in symbols])
            query = f"SELECT * FROM etf_metadata WHERE symbol IN ({placeholders})"
            params = list(symbols)
        else:
            query = "SELECT * FROM etf_metadata"
            params = []

        result = self.conn.execute(query, params).fetchall()
        if not result:
            return pl.DataFrame()

        columns = [desc[0] for desc in self.conn.description]
        return pl.DataFrame(result, schema=columns, orient="row")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_table_count(self, table_name: str) -> int:
        """获取表行数."""
        result = self.conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()
        return result[0] if result else 0

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在."""
        result = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = ? AND table_schema = 'main'",
            [table_name],
        ).fetchone()
        return result[0] > 0 if result else False

    def get_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """获取 ohlcv_daily 表的日期范围."""
        if not self.table_exists("ohlcv_daily"):
            return None, None

        result = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM ohlcv_daily"
        ).fetchone()
        return (result[0], result[1]) if result else (None, None)

    def get_symbols(self) -> list[str]:
        """获取数据库中所有标的代码."""
        if not self.table_exists("ohlcv_daily"):
            return []

        result = self.conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv_daily ORDER BY symbol"
        ).fetchall()
        return [r[0] for r in result]


# ------------------------------------------------------------------
# CLI 入口 (uv run init-db)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 初始化数据库 schema."""
    import sys
    from pathlib import Path

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_root = Path(__file__).resolve().parents[2]
    settings_path = project_root / "config" / "settings.yaml"

    if not settings_path.exists():
        logger.error("settings.yaml not found at %s", settings_path)
        sys.exit(1)

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    db_path = project_root / settings["storage"]["db_path"]
    db = DuckDBManager(str(db_path))
    db.init_database()
    logger.info("Database initialized at %s", db_path)
    db.close()


if __name__ == "__main__":
    main()
